"""读 opencode 的会话数据:新版是 SQLite(opencode.db),旧版是 JSON 文件(storage/message、storage/part)。

只读:SQLite 用 backup 接口拷一份到内存再读(opencode 正在写也不会读到半截,也不会锁住它);JSON 逐个文件读。
统一成 [Session],每个会话按时间排好的 [(消息 id, 消息, [(片段开始毫秒, 片段更新毫秒, 片段)])]。
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Part = Tuple[int, int, dict]


@dataclass
class Session:
    id: str
    title: str = ""
    parent: str = ""
    messages: List[Tuple[str, dict, List[Part]]] = field(default_factory=list)


class StoreError(Exception):
    pass


def default_data_dirs() -> List[pathlib.Path]:
    """opencode 数据目录的候选:$XDG_DATA_HOME/opencode、~/.local/share/opencode。"""
    out = []
    if os.environ.get("XDG_DATA_HOME"):
        out.append(pathlib.Path(os.environ["XDG_DATA_HOME"]) / "opencode")
    out.append(pathlib.Path.home() / ".local" / "share" / "opencode")
    return out


def locate(path: Optional[str]) -> pathlib.Path:
    """给了路径(opencode.db 或数据目录)就用它;没给就按候选找。返回 opencode.db 或数据目录。"""
    cands = [pathlib.Path(path).expanduser()] if path else default_data_dirs()
    for p in cands:
        if p.is_file():
            return p
        if (p / "opencode.db").is_file():
            return p / "opencode.db"
        if (p / "storage" / "message").is_dir():
            return p
    raise StoreError("没找到 opencode 的会话数据(找过:%s)。用 --db 指定 opencode.db 或 opencode 数据目录"
                     % "、".join(str(p) for p in cands))


def load(path: pathlib.Path) -> List[Session]:
    return _load_sqlite(path) if path.is_file() else _load_json(path)


def _part_times(p: dict, fallback: int) -> Tuple[int, int]:
    t = p.get("time") or (p.get("state") or {}).get("time") or {}
    start = t.get("start") or fallback
    return int(start), int(t.get("end") or start)


def _load_sqlite(db: pathlib.Path) -> List[Session]:
    try:
        src = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        mem = sqlite3.connect(":memory:")
        src.backup(mem)
        src.close()
    except sqlite3.Error as exc:
        raise StoreError("读不了会话库 %s:%s" % (db, exc)) from exc
    cols = {r[1] for r in mem.execute("pragma table_info(session)")}
    sessions: Dict[str, Session] = {}
    if cols:
        sel = "id, %s, %s" % ("title" if "title" in cols else "''", "parent_id" if "parent_id" in cols else "''")
        for sid, title, parent in mem.execute("select %s from session" % sel):
            sessions[sid] = Session(sid, title or "", parent or "")
    parts: Dict[str, List[Part]] = {}
    for mid, tc, tu, d in mem.execute("select message_id, time_created, time_updated, data from part order by id"):
        p = json.loads(d)
        start, _ = _part_times(p, tc)
        parts.setdefault(mid, []).append((int(start if p.get("type") in ("reasoning", "text") else tc), int(tu), p))
    for mid, sid, d in mem.execute("select id, session_id, data from message order by time_created, id"):
        sessions.setdefault(sid, Session(sid)).messages.append((mid, json.loads(d), parts.get(mid, [])))
    mem.close()
    return list(sessions.values())


def _read(path: pathlib.Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_json(root: pathlib.Path) -> List[Session]:
    storage = root / "storage"
    sessions: Dict[str, Session] = {}
    for f in sorted((storage / "session").glob("*/*.json")):
        info = _read(f) or {}
        if info.get("id"):
            sessions[info["id"]] = Session(info["id"], info.get("title") or "", info.get("parentID") or "")
    for sdir in sorted((storage / "message").iterdir()) if (storage / "message").is_dir() else []:
        msgs = []
        for f in sdir.glob("*.json"):
            m = _read(f)
            if not m or not m.get("id"):
                continue
            created = int((m.get("time") or {}).get("created") or 0)
            ps = []
            for pf in sorted((storage / "part" / m["id"]).glob("*.json")):
                p = _read(pf)
                if p:
                    start, end = _part_times(p, created)
                    ps.append((start, end, p))
            msgs.append((created, m["id"], m, sorted(ps, key=lambda x: (x[2].get("id") or ""))))
        sess = sessions.setdefault(sdir.name, Session(sdir.name))
        sess.messages = [(mid, m, ps) for _, mid, m, ps in sorted(msgs, key=lambda x: (x[0], x[1]))]
    return list(sessions.values())
