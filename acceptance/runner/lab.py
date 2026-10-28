"""faultlab 这一侧:造故障、边造边记下各场景会话的 pid 与 sql_id、事后读时间线,给占位符与真值用。

faultlab 在技能仓库 tools/faultlab/(只动测试库的模式 fl 与用户 fl_app,实例参数不改)。这里:
  · 起 `faultlab.py run`(20 分钟窗口),等时间线出现「故障窗口开始」;
  · 每 20 秒以 omm 身份查一次 pg_stat_activity 里 application_name 以 fl: 开头的会话,
    记下每个场景的 pid 与 unique_sql_id({pid:K1/root}、{sql_id:S3} 由它们来);
  · 结束后读 ~/.faultlab/timeline.jsonl:窗口起止的库时间、快照号、各场景的自证(真值里「本轮确实注入了」以它为准);
  · 环境事实:版本、连接上限与真实会话数、各类数据的保留期。
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import yaml

TIMELINE = pathlib.Path.home() / ".faultlab" / "timeline.jsonl"
FAULT_CODES = frozenset(["S%d" % i for i in range(1, 8)] + ["K%d" % i for i in range(1, 11)] + ["D"]
                        + ["V%d" % i for i in range(1, 8)] + ["L%d" % i for i in range(1, 6)])
SAMPLE_S = 20
# 场景的设计事实(faultlab 的 schema 与场景代码里写死的),真值直接用
STATIC = {
    "K1": {"depth": "4", "root_app": "fl:K1/root", "object": "fl.acct"},
    "K4": {"object": "fl.acct_child"},
    "V1": {"object": "fl.bloat_hot"},
    "V2": {"object": "fl.xmin_hold"},
    "V5": {"object": "fl.bloat_slow"},
    "V6": {"object": "fl.maint_hist"},
    "V7": {"object": "fl.import_stage"},
}
ACTIVITY_SQL = ("SELECT application_name, pid, unique_sql_id FROM pg_stat_activity "
                "WHERE application_name LIKE 'fl:%' AND sessionid <> 0;")
ENV_SQL = ("SELECT 'version', version() UNION ALL "
           "SELECT name, setting FROM pg_settings WHERE name IN ('max_connections', 'wdr_snapshot_retention_days', "
           "'track_stmt_retention_time', 'asp_retention_days', 'wdr_snapshot_interval') UNION ALL "
           "SELECT 'real_sessions', count(*)::text FROM pg_stat_activity WHERE sessionid <> 0;")


def omm_runner(faultlab_dir: pathlib.Path) -> Callable[[str], str]:
    """借 faultlab 的 omm_sql(docker exec 进测试库容器,以 omm 跑 gsql)。"""
    if str(faultlab_dir) not in sys.path:
        sys.path.insert(0, str(faultlab_dir))
    from lab import db  # type: ignore  # noqa: E402  —— faultlab 自己的包

    return lambda sql: db.omm_sql(sql, timeout_s=60)


def parse_rows(out: str, ncol: int) -> List[List[str]]:
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == ncol:
            rows.append([p.strip() for p in parts])
    return rows


class Sampler:
    """故障窗口里定时抽样实验会话。"""

    def __init__(self, sql: Callable[[str], str]):
        self.sql = sql
        self.pids: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.sql_ids: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        self.env: Dict[str, str] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.errors: List[str] = []

    def sample(self) -> None:
        try:
            rows = parse_rows(self.sql(ACTIVITY_SQL), 3)
        except Exception as e:                       # noqa: BLE001 —— 抽样失败不中断跑题,记下来
            self.errors.append(str(e)[:200])
            return
        with self._lock:
            for app, pid, sid in rows:
                self.pids[app][pid] += 1
                if sid and sid not in ("0", ""):
                    self.sql_ids[app][sid] += 1

    def sample_env(self) -> None:
        try:
            self.env.update({k: v for k, v in parse_rows(self.sql(ENV_SQL), 2)})
        except Exception as e:                       # noqa: BLE001
            self.errors.append(str(e)[:200])

    def loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(SAMPLE_S)

    def start(self) -> None:
        threading.Thread(target=self.loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def pid(self, app: str) -> str:
        with self._lock:
            c = self.pids.get(app)
            return c.most_common(1)[0][0] if c else ""

    def sql_id(self, code: str) -> str:
        """场景的主语句:该场景各会话里出现次数最多的 unique_sql_id。"""
        total: collections.Counter = collections.Counter()
        with self._lock:
            for app, c in self.sql_ids.items():
                if app.split(":", 1)[-1].split("/", 1)[0] == code:
                    total.update(c)
        return total.most_common(1)[0][0] if total else ""

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"pids": {a: dict(c) for a, c in self.pids.items()},
                    "sql_ids": {a: dict(c) for a, c in self.sql_ids.items()}, "env": dict(self.env),
                    "errors": self.errors[-5:]}


def read_timeline(path: pathlib.Path = TIMELINE) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """时间线 → 窗口起止(库时间)、快照号、各场景最后一次自证、起不来的场景。"""
    out: Dict[str, Any] = {"start": "", "end": "", "snapshots": [], "probes": {}, "failed": []}
    for r in rows:
        ev, detail = str(r.get("event") or ""), str(r.get("detail") or "")
        if ev == "故障窗口开始":
            out["start"] = r.get("db_time") or ""
        elif ev == "故障窗口结束":
            out["end"] = r.get("db_time") or ""
        elif ev == "快照号":
            out["snapshots"] = [int(x) for x in re.findall(r"\d+", detail)]
        elif ev == "场景起不来":
            out["failed"].append(detail)
        elif ev.startswith("  ") and ev.strip():
            out["probes"][ev.strip()] = detail
    return out


def expected(faultlab_dir: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    """expected.yaml 里每个场景的标题与各技能应报出什么(给裁判的真值说明)。"""
    data = yaml.safe_load((faultlab_dir / "expected.yaml").read_text(encoding="utf-8")) or {}
    out = {}
    for s in data.get("scenarios") or []:
        if isinstance(s, dict) and s.get("code"):
            out[str(s["code"])] = {"title": s.get("title") or "", "expect": s.get("expect") or []}
    return out


class LabRun:
    """一次 faultlab run 的生命周期。"""

    def __init__(self, faultlab_dir: pathlib.Path, python: pathlib.Path, log: pathlib.Path):
        self.dir, self.python, self.log = faultlab_dir, python, log
        self.proc: Optional[subprocess.Popen] = None
        self.t0 = 0.0

    def start(self) -> None:
        self.t0 = time.time()
        fh = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen([str(self.python), str(self.dir / "faultlab.py"), "run"], cwd=str(self.dir),
                                     stdout=fh, stderr=subprocess.STDOUT)

    def window_started(self) -> bool:
        if not TIMELINE.is_file() or TIMELINE.stat().st_mtime < self.t0:
            return False
        return any(r.get("event") == "故障窗口开始" for r in read_timeline())

    def wait_window(self, timeout_s: float = 600, sleep: Callable[[float], None] = time.sleep) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.window_started():
                return True
            if self.proc and self.proc.poll() is not None:
                return False
            sleep(5)
        return False

    def wait_done(self) -> int:
        return self.proc.wait() if self.proc else -1

    def stop(self) -> None:
        """跑题中途出错时停掉 faultlab,并让它自己清干净(造故障的会话、锁、两阶段事务、复制槽)。"""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            subprocess.run([str(self.python), str(self.dir / "faultlab.py"), "cleanup"], cwd=str(self.dir),
                           capture_output=True, text=True, timeout=600)
