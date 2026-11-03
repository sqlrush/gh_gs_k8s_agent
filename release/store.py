"""发版台的状态:SQLite 一个文件(/state/release.db,权限 0600)。

- 资源配置:当前一份 + 每次渲染 / 保存记一个配置版本(回滚 = 回到上一个配置版本)。
- 密钥:发版台**自己要用的**(镜像仓库密码、白名单只读账号)存值;要写进集群的(模型 Key、信任凭据、SM2 私钥)
  写进 Secret 后这里只留指纹与时间。接口永远不返回值。
- 发布:run / step / 日志 / 关卡 / 操作记录 / 扫描结果。关卡持久化:发版台重启后照样停在那里。
- **脱敏**:日志与操作记录写入前把所有已知密钥值替换成「●●●」—— 任何一步不小心把值打进日志,落盘的也只是占位。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config_versions (id INTEGER PRIMARY KEY, at REAL, note TEXT, config TEXT);
CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT, fingerprint TEXT, set_at REAL, kept INTEGER);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, kind TEXT, from_v TEXT, to_v TEXT, status TEXT, created REAL,
  finished REAL, plan TEXT, config_version INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS steps (run_id INTEGER, idx INTEGER, step TEXT, status TEXT, started REAL, finished REAL,
  tries INTEGER DEFAULT 0, detail TEXT DEFAULT '', error TEXT DEFAULT '', subs TEXT DEFAULT '[]', PRIMARY KEY (run_id, idx));
CREATE TABLE IF NOT EXISTS logs (seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, step TEXT, at REAL, level TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS gates (id INTEGER PRIMARY KEY, run_id INTEGER, step TEXT, kind TEXT, payload TEXT, created REAL,
  resolved REAL, choice TEXT, consumed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, action TEXT, target TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, at REAL, status TEXT, items TEXT);
"""
MASK = "●●●"
ACTIVE = ("running", "waiting", "paused")


class _Rows:
    """已经取完的查询结果:像游标一样用(fetchone / 迭代 / lastrowid),但不再碰连接。"""

    def __init__(self, rows: List[sqlite3.Row], lastrowid: Optional[int]):
        self.rows, self.lastrowid = rows, lastrowid

    def fetchone(self) -> Optional[sqlite3.Row]:
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class Store:
    def __init__(self, path: pathlib.Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if not path.exists():
            os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(path, 0o600)
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(gates)")}
        if "consumed" not in cols:                  # 早期的状态库没有这一列
            self.db.execute("ALTER TABLE gates ADD COLUMN consumed INTEGER DEFAULT 0")
        self._known: set = {r["value"] for r in self.db.execute("SELECT value FROM secrets WHERE value IS NOT NULL")
                            if r["value"]}

    # -- 基础 -------------------------------------------------------------------------------------

    def _x(self, sql: str, args: Iterable[Any] = ()) -> "_Rows":
        """执行并在锁内取完结果。连接被接口线程与执行引擎线程共用:锁外再读游标,另一个线程同时执行时游标会被打乱
        (11-03 引擎单测偶发 IndexError 抓到的)。"""
        with self._lock:
            cur = self.db.execute(sql, tuple(args))
            return _Rows(cur.fetchall() if cur.description else [], cur.lastrowid)

    def redact(self, text: str) -> str:
        out = str(text)
        for v in sorted(self._known, key=len, reverse=True):
            if len(v) >= 4:
                out = out.replace(v, MASK)
        return out

    def remember_secret(self, value: str) -> None:
        """进程里临时拿到的密钥值(例如冒烟时现读的信任凭据)也要脱敏,但不落盘。"""
        if value:
            self._known.add(value)

    def kv_get(self, k: str, default: Any = None) -> Any:
        row = self._x("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def kv_set(self, k: str, v: Any) -> None:
        self._x("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, json.dumps(v)))

    # -- 资源配置 ----------------------------------------------------------------------------------

    def config(self) -> Dict[str, Any]:
        return self.kv_get("config", {})

    def save_config(self, cfg: Dict[str, Any], note: str = "") -> int:
        self.kv_set("config", cfg)
        cur = self._x("INSERT INTO config_versions (at, note, config) VALUES (?, ?, ?)",
                      (self.clock(), note, json.dumps(cfg, ensure_ascii=False)))
        return int(cur.lastrowid)

    def config_version(self, vid: int) -> Optional[Dict[str, Any]]:
        row = self._x("SELECT config FROM config_versions WHERE id = ?", (vid,)).fetchone()
        return json.loads(row["config"]) if row else None

    def config_versions(self, limit: int = 20) -> List[dict]:
        return [dict(r) for r in self._x("SELECT id, at, note FROM config_versions ORDER BY id DESC LIMIT ?", (limit,))]

    # -- 密钥 ----------------------------------------------------------------------------------------

    def set_secret(self, name: str, value: str, keep: bool) -> str:
        """keep=True:发版台自己要用,存值;False:已写进集群,只留指纹。返回指纹。"""
        fp = fingerprint(value)
        self._x("INSERT INTO secrets (name, value, fingerprint, set_at, kept) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, fingerprint = excluded.fingerprint, "
                "set_at = excluded.set_at, kept = excluded.kept",
                (name, value if keep else None, fp, self.clock(), int(keep)))
        self._known.add(value)
        return fp

    def secret(self, name: str) -> str:
        """只给发版台内部用(推镜像、连只读账号)。不经接口。"""
        row = self._x("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
        return (row["value"] or "") if row else ""

    def secret_marks(self) -> Dict[str, dict]:
        return {r["name"]: {"fingerprint": r["fingerprint"], "set_at": r["set_at"], "kept": bool(r["kept"])}
                for r in self._x("SELECT name, fingerprint, set_at, kept FROM secrets")}

    # -- 发布 ----------------------------------------------------------------------------------------

    def create_run(self, kind: str, from_v: str, to_v: str, plan: List[dict], config_version: Optional[int]) -> int:
        cur = self._x("INSERT INTO runs (kind, from_v, to_v, status, created, plan, config_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (kind, from_v, to_v, "pending", self.clock(), json.dumps(plan, ensure_ascii=False), config_version))
        rid = int(cur.lastrowid)
        for i, st in enumerate(plan):
            self._x("INSERT INTO steps (run_id, idx, step, status, subs) VALUES (?, ?, ?, ?, ?)",
                    (rid, i, st["id"], "skipped" if st.get("skip") else "pending",
                     json.dumps([{"label": s, "st": "pending", "note": ""} for s in st.get("subs") or []], ensure_ascii=False)))
        return rid

    def run(self, rid: int) -> Optional[dict]:
        row = self._x("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["plan"] = json.loads(out["plan"] or "[]")
        out["steps"] = [dict(s, subs=json.loads(s["subs"] or "[]"))
                        for s in self._x("SELECT * FROM steps WHERE run_id = ? ORDER BY idx", (rid,))]
        out["gate"] = self.open_gate(rid)
        return out

    def runs(self, limit: int = 50) -> List[dict]:
        return [dict(r) for r in self._x("SELECT id, kind, from_v, to_v, status, created, finished FROM runs "
                                         "ORDER BY id DESC LIMIT ?", (limit,))]

    def update_run(self, rid: int, **fields: Any) -> None:
        if "error" in fields:
            fields["error"] = self.redact(fields["error"] or "")
        cols = ", ".join("%s = ?" % k for k in fields)
        self._x("UPDATE runs SET %s WHERE id = ?" % cols, list(fields.values()) + [rid])

    def update_step(self, rid: int, idx: int, **fields: Any) -> None:
        if "subs" in fields:
            fields["subs"] = json.dumps(fields["subs"], ensure_ascii=False)
        for k in ("detail", "error"):
            if k in fields:
                fields[k] = self.redact(fields[k] or "")
        cols = ", ".join("%s = ?" % k for k in fields)
        self._x("UPDATE steps SET %s WHERE run_id = ? AND idx = ?" % cols, list(fields.values()) + [rid, idx])

    def active_run(self) -> Optional[int]:
        row = self._x("SELECT id FROM runs WHERE status IN (%s) ORDER BY id DESC LIMIT 1" % ",".join("?" * len(ACTIVE)),
                      ACTIVE).fetchone()
        return int(row["id"]) if row else None

    def mark_interrupted(self) -> List[int]:
        """发版台启动时调用:上次正在跑的步骤标成「被中断」,整次发布停在「暂停」,由运维选续跑或回滚。关卡原样保留。"""
        ids = [int(r["id"]) for r in self._x("SELECT id FROM runs WHERE status = 'running'")]
        for rid in ids:
            self._x("UPDATE steps SET status = 'interrupted' WHERE run_id = ? AND status = 'running'", (rid,))
            self._x("UPDATE runs SET status = 'paused' WHERE id = ?", (rid,))
            self.log(rid, "", "w", "发版台重启:上次正在执行的步骤被中断,请选择从这一步续跑或回滚")
        return ids

    # -- 日志 / 关卡 / 记录 ---------------------------------------------------------------------------

    def log(self, rid: int, step: str, level: str, text: str) -> int:
        cur = self._x("INSERT INTO logs (run_id, step, at, level, text) VALUES (?, ?, ?, ?, ?)",
                      (rid, step, self.clock(), level, self.redact(text)))
        return int(cur.lastrowid)

    def logs(self, rid: int, after: int = 0, step: str = "", limit: int = 500) -> List[dict]:
        sql, args = "SELECT seq, step, at, level, text FROM logs WHERE run_id = ? AND seq > ?", [rid, after]
        if step:
            sql, args = sql + " AND step = ?", args + [step]
        return [dict(r) for r in self._x(sql + " ORDER BY seq LIMIT ?", args + [limit])]

    def open_gate(self, rid: int) -> Optional[dict]:
        row = self._x("SELECT * FROM gates WHERE run_id = ? AND resolved IS NULL ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        return dict(row, payload=json.loads(row["payload"] or "{}")) if row else None

    def add_gate(self, rid: int, step: str, kind: str, payload: Optional[dict] = None) -> int:
        cur = self._x("INSERT INTO gates (run_id, step, kind, payload, created) VALUES (?, ?, ?, ?, ?)",
                      (rid, step, kind, json.dumps(payload or {}, ensure_ascii=False), self.clock()))
        return int(cur.lastrowid)

    def pending_gate(self, rid: int, step: str, kind: str) -> Optional[dict]:
        """本步骤这一类关卡里还没被消费的那个(可能已有运维的选择、只是等着的线程还没取走)。"""
        row = self._x("SELECT * FROM gates WHERE run_id = ? AND step = ? AND kind = ? AND consumed = 0 ORDER BY id DESC LIMIT 1",
                      (rid, step, kind)).fetchone()
        return dict(row, payload=json.loads(row["payload"] or "{}")) if row else None

    def consume_gate(self, gid: int) -> None:
        self._x("UPDATE gates SET consumed = 1 WHERE id = ?", (gid,))

    def resolve_gate(self, gid: int, choice: str) -> None:
        self._x("UPDATE gates SET resolved = ?, choice = ? WHERE id = ? AND resolved IS NULL", (self.clock(), choice, gid))

    def gate(self, gid: int) -> Optional[dict]:
        row = self._x("SELECT * FROM gates WHERE id = ?", (gid,)).fetchone()
        return dict(row, payload=json.loads(row["payload"] or "{}")) if row else None

    def audit(self, action: str, target: str, detail: str = "") -> None:
        self._x("INSERT INTO audit (at, action, target, detail) VALUES (?, ?, ?, ?)",
                (self.clock(), action, self.redact(target), self.redact(detail)))

    def audits(self, limit: int = 200) -> List[dict]:
        return [dict(r) for r in self._x("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))]

    def save_scan(self, status: str, items: List[dict]) -> int:
        cur = self._x("INSERT INTO scans (at, status, items) VALUES (?, ?, ?)",
                      (self.clock(), status, self.redact(json.dumps(items, ensure_ascii=False))))
        return int(cur.lastrowid)

    def last_scan(self) -> Optional[dict]:
        row = self._x("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row, items=json.loads(row["items"])) if row else None

    def close(self) -> None:
        with self._lock:
            self.db.close()
