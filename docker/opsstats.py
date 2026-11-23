"""状态服务的运维统计(资源大盘,2026-11-18):请求计数、24 小时趋势、NAS 用量。

只由网关调(运维签名,见 sbxtoken.ops_sign),沙箱的令牌调不到这里。

- 请求计数:网关各副本把攒下的「工号 × 环境 → 交互请求次数」每 30 秒报一次(增量),这里累加成「今天」各工号的次数
  (本地时区的日界,过零点清零),同时按半小时一个点累计进 24 小时趋势。
- 沙箱台数:网关的清扫器每轮报一次运行 / 暂停台数;两个网关副本各自清扫、报的是同一份全量,按时间点取最后一次,不相加。
- NAS 用量:users/、admins/、kb/ 三个目录的条目数(直接子项)、文件数、字节数,后台每 5 分钟算一次。
  按文件描述符逐级打开、不跟随链接(本服务挂着全体用户的目录,不能被谁预埋的链接带到别处去数)。
- 落盘:内存里的计数与趋势每分钟(有变化时)原子写到 NAS 的 .ops/usage.json,重启后恢复;超过 24 小时的点丢掉。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Callable, Dict, Iterable, Optional, Tuple

import fdtree

BUCKET_S = 1800
TREND_N = 48
NAS_EVERY_S = 300
SAVE_EVERY_S = 60
NAS_DIRS = ("users", "admins", "kb")
STATE_DIR, STATE_FILE = ".ops", "usage.json"
KINDS = ("runtime", "kb-import")
MAX_USERS = 100000
MAX_COUNT = 100000                     # 一次上报里单项的上限(30 秒攒不出这么多交互请求)
TOP_N = 500
_USER = re.compile(r"^[a-z0-9]([a-z0-9-]{0,52}[a-z0-9])?$")       # 与 gateway/identity.py 的工号规则一致


def du(dir_fd: int) -> Tuple[int, int]:
    """一棵目录树里的文件数与字节数。不跟随链接;打不开的子目录跳过(计数偏小,不报错)。"""
    files = total = 0
    stack = [os.dup(dir_fd)]
    while stack:
        fd = stack.pop()
        try:
            with os.scandir(fd) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(os.open(e.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd))
                        elif e.is_file(follow_symlinks=False):
                            files += 1
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            pass
        finally:
            os.close(fd)
    return files, total


def scan_nas(nas_fd: int, dirs: Iterable[str] = NAS_DIRS) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for name in dirs:
        try:
            dfd = fdtree.open_path(nas_fd, name, create=False)
        except OSError:
            dfd = None
        if dfd is None:
            out[name] = {"exists": False, "entries": 0, "files": 0, "bytes": 0}
            continue
        try:
            entries = len([n for n in os.listdir(dfd) if not n.startswith(".")])
            files, total = du(dfd)
        finally:
            os.close(dfd)
        out[name] = {"exists": True, "entries": entries, "files": files, "bytes": total}
    return out


class OpsStats:
    def __init__(self, nas_fd: int, log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 localtime: Callable[[float], time.struct_time] = time.localtime):
        self.nas_fd, self.log, self.clock, self.localtime = nas_fd, log, clock, localtime
        self._lock = threading.Lock()
        self._day = self._today(clock())
        self._by: Dict[Tuple[str, str], int] = {}
        self._buckets: Dict[int, dict] = {}
        self._nas: Optional[dict] = None
        self._dirty = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.load()

    # -- 时间 -----------------------------------------------------------------------------------------

    def _today(self, now: float) -> str:
        return time.strftime("%Y-%m-%d", self.localtime(now))

    @staticmethod
    def bucket(now: float) -> int:
        return int(now // BUCKET_S * BUCKET_S)

    def _roll(self, now: float) -> None:
        """过了零点清零今天的计数;丢掉 24 小时以外的点。调用方持锁。"""
        day = self._today(now)
        if day != self._day:
            self._day, self._by, self._dirty = day, {}, True
        oldest = self.bucket(now) - (TREND_N - 1) * BUCKET_S
        for t in [t for t in self._buckets if t < oldest]:
            del self._buckets[t]

    # -- 上报 -----------------------------------------------------------------------------------------

    def add_requests(self, items: object, now: Optional[float] = None) -> Tuple[int, dict]:
        """items:[{"u": 工号, "k": 环境, "n": 次数}]。整批先校验,有一项不合法就整批拒绝(不写半批)。"""
        if not isinstance(items, list) or len(items) > 20000:
            return 400, {"error": "items 要是列表(至多 20000 项)"}
        rows = []
        for it in items:
            if not isinstance(it, dict):
                return 400, {"error": "items 的每一项要是对象"}
            u, k, n = it.get("u"), it.get("k"), it.get("n")
            if not isinstance(u, str) or not _USER.match(u) or k not in KINDS:
                return 400, {"error": "工号或环境不合法"}
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_COUNT:
                return 400, {"error": "次数要是 0~%d 的整数" % MAX_COUNT}
            rows.append((u, k, n))
        now = self.clock() if now is None else now
        with self._lock:
            self._roll(now)
            added = 0
            for u, k, n in rows:
                if not n:
                    continue
                if (u, k) not in self._by and len(self._by) >= MAX_USERS:
                    continue
                self._by[(u, k)] = self._by.get((u, k), 0) + n
                added += n
            b = self._buckets.setdefault(self.bucket(now), {})
            b["requests"] = b.get("requests", 0) + added
            self._dirty = True
        return 200, {"added": added}

    def set_fleet(self, running: object, paused: object, now: Optional[float] = None) -> Tuple[int, dict]:
        for v in (running, paused):
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1000000:
                return 400, {"error": "running / paused 要是非负整数"}
        now = self.clock() if now is None else now
        with self._lock:
            self._roll(now)
            b = self._buckets.setdefault(self.bucket(now), {})
            b["running"], b["paused"] = running, paused
            self._dirty = True
        return 200, {"ok": True}

    # -- 读 -------------------------------------------------------------------------------------------

    def snapshot(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        with self._lock:
            self._roll(now)
            rows = sorted(self._by.items(), key=lambda kv: (-kv[1], kv[0]))
            cur = self.bucket(now)
            trend = []
            for i in range(TREND_N - 1, -1, -1):
                t = cur - i * BUCKET_S
                b = self._buckets.get(t) or {}
                trend.append({"t": t, "requests": b.get("requests", 0), "running": b.get("running"), "paused": b.get("paused")})
            return {
                "date": self._day,
                "requests": {"total": sum(n for _k, n in rows), "users": len({u for (u, _k), _n in rows}),
                             "by_user": [{"user_id": u, "kind": k, "count": n} for (u, k), n in rows[:TOP_N]],
                             "truncated": len(rows) > TOP_N},
                "trend": trend, "bucket_seconds": BUCKET_S,
                "nas": dict(self._nas) if self._nas else None,
            }

    # -- NAS 用量 -------------------------------------------------------------------------------------

    def refresh_nas(self) -> dict:
        t0 = self.clock()
        dirs = scan_nas(self.nas_fd)
        got = {"at": t0, "took_s": round(max(0.0, self.clock() - t0), 2), "dirs": dirs}
        with self._lock:
            self._nas = got
        return got

    # -- 落盘 -----------------------------------------------------------------------------------------

    def save(self) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            doc = {"v": 1, "date": self._day, "by": [[u, k, n] for (u, k), n in self._by.items()],
                   "buckets": {str(t): b for t, b in self._buckets.items()}}
            self._dirty = False
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        dfd = fdtree.open_path(self.nas_fd, STATE_DIR, create=True, mode=0o700)
        try:
            fdtree.write_small(dfd, STATE_FILE, data)
        except OSError:
            with self._lock:
                self._dirty = True
            raise
        finally:
            os.close(dfd)
        return True

    def load(self) -> None:
        try:
            dfd = fdtree.open_path(self.nas_fd, STATE_DIR, create=False)
        except OSError:
            dfd = None
        if dfd is None:
            return
        try:
            raw = fdtree.read_small(dfd, STATE_FILE, limit=16 << 20)
        except OSError as exc:
            self.log("运维统计:读 %s/%s 失败,从零开始:%s" % (STATE_DIR, STATE_FILE, exc))
            return
        finally:
            os.close(dfd)
        if not raw:
            return
        try:
            doc = json.loads(raw)
            by = {(str(u), str(k)): int(n) for u, k, n in doc.get("by") or [] if _USER.match(str(u)) and k in KINDS}
            buckets = {int(t): {kk: int(v) for kk, v in (b or {}).items() if kk in ("requests", "running", "paused")}
                       for t, b in (doc.get("buckets") or {}).items()}
            day = str(doc.get("date") or "")
        except (ValueError, TypeError, AttributeError) as exc:
            self.log("运维统计:%s/%s 内容不对,从零开始:%s" % (STATE_DIR, STATE_FILE, exc))
            return
        now = self.clock()
        with self._lock:
            self._buckets = buckets
            self._by = by if day == self._today(now) else {}
            self._roll(now)
        self.log("运维统计:已从 NAS 恢复(今天 %d 项计数、%d 个趋势点)" % (len(self._by), len(self._buckets)))

    # -- 后台 -----------------------------------------------------------------------------------------

    def start(self, nas_every_s: int = NAS_EVERY_S, save_every_s: int = SAVE_EVERY_S, first_scan_s: float = 5.0) -> None:
        def loop() -> None:
            next_scan = time.time() + first_scan_s
            next_save = time.time() + save_every_s
            while not self._stop.wait(1.0):
                now = time.time()
                if now >= next_scan:
                    next_scan = now + nas_every_s
                    try:
                        got = self.refresh_nas()
                        if got["took_s"] > 60:
                            self.log("运维统计:算一次 NAS 用量用了 %.0f 秒" % got["took_s"])
                    except Exception as exc:          # noqa: BLE001 —— 后台线程不能因为一次出错就停
                        self.log("运维统计:算 NAS 用量失败:%s" % exc)
                if now >= next_save:
                    next_save = now + save_every_s
                    try:
                        self.save()
                    except Exception as exc:          # noqa: BLE001
                        self.log("运维统计:落盘失败,下一分钟再试:%s" % exc)
            try:
                self.save()
            except Exception:                          # noqa: BLE001
                pass
        self._thread = threading.Thread(target=loop, name="opsstats", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
