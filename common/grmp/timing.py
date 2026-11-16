"""GRMP 每次调用的耗时记录(v1.4.14):给「交互耗时报告」用,拆出一次工具调用里 GRMP 与数据库占了多少。

只记:开始时间、用时、接口(取命令清单 / 执行脚本)、脚本名、执行库、成败、返回行数与字节数、调用它的技能脚本。
**不记**参数值、结果内容、中间件地址、令牌 —— 这份日志可以原样交给我方分析。

写在本机诊断日志目录,规则与 grmp-errors.log 相同(见 client._log_raw):设了 GSDB_HOME / GDAA_HOME,或默认状态目录
(技能目录下的 common/)存在时才写;写不了(只读盘、权限)就算了 —— 计时只是旁路,绝不影响取数。
一行一条 JSON;超过 2 MB 滚动成 .1(只留一份,约两万次调用)。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, Dict, Optional

from ..config import state_dir

TIMING_LOG = os.path.join("logs", "grmp-timing.log")
_MAX = 2 << 20

LIST = "list"        # 接口一:取命令清单
INVOKE = "invoke"    # 接口二:执行登记脚本


def log_path():
    return state_dir() / TIMING_LOG


def _enabled() -> bool:
    return bool(os.environ.get("GSDB_HOME") or os.environ.get("GDAA_HOME") or state_dir().is_dir())


def _caller() -> str:
    """调用方:技能名/脚本名(如 gaussdb-wdr/wdr.py),报告据此把这次调用对到会话里的那次工具调用。"""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    parts = argv0.split(os.sep)
    if len(parts) >= 3 and parts[-2] == "scripts":
        return "%s/%s" % (parts[-3], parts[-1])
    return os.path.basename(argv0)


def _summary(kind: str, body: Any) -> Dict[str, Any]:
    """从响应里只取成败、行数、字节数。"""
    if not isinstance(body, dict):
        return {"ok": False}
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    if kind == LIST:
        items = result.get("list")
        return {"ok": body.get("code") == "0", "rows": len(items) if isinstance(items, list) else None}
    data = result.get("data")
    out: Dict[str, Any] = {"ok": body.get("status") == "finished" and "result" in body,
                           "status": str(body.get("status") or "")}
    if isinstance(data, list):
        out["rows"] = len(data)
        out["bytes"] = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return out


def _write(wall: float, mono: float, kind: str, script: str, db: str, info: Dict[str, Any]) -> None:
    try:
        if not _enabled():
            return
        rec = {"ts": round(wall, 3), "ms": int((time.monotonic() - mono) * 1000), "kind": kind, "script": script,
               "db": db, "pid": os.getpid(), "caller": _caller()}
        rec.update(info)
        path = log_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _MAX:
            path.replace(path.with_name(path.name + ".1"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001  计时是旁路:任何问题(盘满、权限、编码)都不能影响取数
        pass


def call(fn: Callable[[], Any], kind: str, script: str, db: Optional[str] = "") -> Any:
    """执行一次 HTTP 调用并记下用时;成功失败都记,异常原样抛出。"""
    wall, mono = time.time(), time.monotonic()
    try:
        body = fn()
    except Exception as exc:
        _write(wall, mono, kind, script, db or "", {"ok": False, "error": type(exc).__name__})
        raise
    _write(wall, mono, kind, script, db or "", _summary(kind, body))
    return body
