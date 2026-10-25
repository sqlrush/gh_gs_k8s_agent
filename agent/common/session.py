"""当前会话选中的数据库连接 —— **按句柄分文件**。

gaussdb-login 选定一个连接后写在这里，其余 skill 从这里取。存的是**整条连接定义**而不只是名字：
api 模式下的连接是登录时按用户给的库名现场构造的，配置文件里根本没有它，只存名字会导致下一个
skill 找不到。不存密码：api 模式的令牌、gsql 模式的口令仍各走各的通道，会话文件即使被看到也拿不到凭据。

**为什么按句柄分文件（2026-09-07 现场缺陷）**：客户多个用户共用一个沙箱、一个 GSDB_HOME。原先只有
一个 session.yaml，谁最后登录所有人就连谁的库——退出码 0、不报错、报告抬头写着别人的库，静默串库。
现在每次登录得到一个随机句柄，写 `sessions/<句柄>.yaml`；其余 skill 用 `--session <句柄>`（或环境变量
GSDB_SESSION，health 派生的子 skill 靠它继承）指名要哪一份。

**会话只属于创建它的对话（2026-09-11 现场反馈的越权）**：原先「只有一个会话时自动用它、多个会话时列出候选
让用户挑」——用户 C 进来什么都不带就跑在 A 的库上，或者从候选里挑走 B 的句柄。现在没句柄就是没登录：
不自动选、不列候选、不报数量，只回一句「本对话未登录」；句柄只能来自本对话里 login 的输出，或平台按用户
注入的 GSDB_SESSION。同沙箱同 OS 用户下别人的会话文件在文件系统层面仍读得到——那要靠平台注入身份或一人一沙箱，
这里堵的是「看得见、顺手用」两条。

闲置超过 TTL（默认 12 小时，GSDB_SESSION_TTL_HOURS 可调）的会话自动清理。旧的单文件 session.yaml 不再认
（它也是谁都能用的口子），登录时顺手删掉。文件权限 0600。
"""
from __future__ import annotations

import os
import pathlib
import re
import secrets
import time
from dataclasses import dataclass, replace
from typing import List, Optional

import yaml

from .config import Connection, ConfigError, api_endpoint, ensure_dir, state_dir, validate

_DIRNAME = "sessions"
_LEGACY_FILENAME = "session.yaml"
ENV_HANDLE = "GSDB_SESSION"
ENV_TTL = "GSDB_SESSION_TTL_HOURS"
DEFAULT_TTL_HOURS = 12
HANDLE_RE = re.compile(r"^[a-z0-9]{4,16}$")
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # 去掉 0/o、1/l/i 这些肉眼易混的字符
_HANDLE_LEN = 12                                 # 31^12 ≈ 8e17,猜不到;句柄就是这个对话访问那个库的凭证

NOT_LOGGED_IN = ("本对话未登录。请先运行 gaussdb-login 登录要访问的库，之后每条命令带上它输出的 "
                 "`--session <句柄>`（平台也可以按用户设置环境变量 GSDB_SESSION）。")

# 会话文件里允许出现的键。多余的键一律拒绝 —— 手工改这个文件时写错一个键名
# （比如把 data_ip 写成 dataip），静默忽略的话会连到错误的实例上去。
_ALLOWED = frozenset({"name", "type", "host", "port", "database", "user",
                      "sslmode", "driver", "data_ip", "app"})

_selected: Optional[str] = None      # 本进程用 use() 选中的句柄（来自 --session）


@dataclass(frozen=True)
class SessionInfo:
    handle: str
    conn: Connection
    last_used: float
    path: pathlib.Path


# --- 句柄 ---------------------------------------------------------------------

def ttl_seconds() -> int:
    raw = os.environ.get(ENV_TTL, "").strip()
    try:
        hours = float(raw) if raw else float(DEFAULT_TTL_HOURS)
    except ValueError:
        hours = float(DEFAULT_TTL_HOURS)
    return int(hours * 3600)


def _check_handle(handle) -> str:
    """句柄同时是文件名的一部分：形状不合法一律拒，这里就是路径穿越的闸。"""
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        raise ConfigError(
            "会话句柄 %r 不合法：只能是 4~16 位小写字母或数字，即 gaussdb-login 登录成功时输出的那一串。"
            % (handle,))
    return handle


def new_handle() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_HANDLE_LEN))


def use(handle: Optional[str]) -> None:
    """本进程选中一个句柄（来自 --session）。传 None / 空串清除选择。"""
    global _selected
    _selected = _check_handle(handle) if handle else None


def selected() -> Optional[str]:
    """当前指名的句柄：--session > 环境变量 GSDB_SESSION；都没有则 None。"""
    if _selected:
        return _selected
    env = os.environ.get(ENV_HANDLE, "").strip()
    return _check_handle(env) if env else None


# --- 文件 ---------------------------------------------------------------------

def _dir() -> pathlib.Path:
    return state_dir() / _DIRNAME


def _legacy_path() -> pathlib.Path:
    return state_dir() / _LEGACY_FILENAME


def path_for(handle: str) -> pathlib.Path:
    return _dir() / ("%s.yaml" % _check_handle(handle))


def _write(path: pathlib.Path, payload: dict) -> None:
    # 先写临时文件再改名：中途失败不会留下半个会话文件，
    # 而半个文件解析出来可能正好是另一个合法连接
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _read(path: pathlib.Path) -> Connection:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError("解析会话文件 %s 失败：%s" % (path, exc)) from exc
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("会话文件 %s 是空的。删掉它重新运行 gaussdb-login 即可。" % path)
    unknown = set(raw) - _ALLOWED
    if unknown:
        raise ConfigError(
            "会话文件 %s 里有无法识别的键：%s。\n"
            "静默忽略它们的话，写错一个键名（比如 data_ip 写成 dataip）"
            "会让下一次取数连到错误的实例上去，而且不报错。\n"
            "删掉这个文件重新运行 gaussdb-login 即可。"
            % (path, "、".join(sorted(unknown))))
    conn = Connection(
        name=raw.get("name", ""), type=raw.get("type", ""),
        host=raw.get("host", ""), port=raw.get("port", 0),
        database=raw.get("database", ""), user=raw.get("user", ""),
        sslmode=raw.get("sslmode", "") or "",
        driver=raw.get("driver", "gsql") or "gsql",
        data_ip=raw.get("data_ip", "") or "",
        app=raw.get("app", "") or "",
    )
    if conn.driver == "grmp":
        # 会话文件只是登录那一刻的快照。中间件地址属于环境配置,不属于会话:
        # 地址变更后老句柄仍要能用,所以每次读会话都从当前配置取(环境变量 > config.yaml)。
        try:
            ep = api_endpoint()
        except ConfigError:
            ep = None                         # 没配 api_connection 的老环境:保留文件里的值
        if ep is not None:
            conn = replace(conn, host=ep.resolve_host(), port=ep.port)
    validate(conn)
    return conn


def _touch(path: pathlib.Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def target_of(conn: Connection) -> str:
    """报告 / 清单里标识目标的写法：api 模式是 dataIp，gsql 模式是 host:port。"""
    return conn.data_ip or "%s:%s" % (conn.host, conn.port)


def save(conn: Connection, handle: Optional[str] = None) -> str:
    """把选中的连接写成一份新的会话文件，返回句柄。不覆盖任何已有会话。"""
    validate(conn)
    ensure_dir()
    d = _dir()
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    if handle:
        handle = _check_handle(handle)
    else:
        handle = new_handle()
        while path_for(handle).exists():
            handle = new_handle()
    payload = {
        "name": conn.name, "type": conn.type, "host": conn.host,
        "port": conn.port, "database": conn.database, "user": conn.user,
        "sslmode": conn.sslmode, "driver": conn.driver,
        "data_ip": conn.data_ip, "app": conn.app,
    }
    _write(path_for(handle), payload)
    legacy = _legacy_path()                     # 旧版单文件:谁都能用的口子,见到就删
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass
    return handle


def list_sessions(prune: bool = True) -> List[SessionInfo]:
    """现存的会话，按最后使用时间倒序。过期的顺手删掉（prune=False 时只跳过）。"""
    d = _dir()
    if not d.is_dir():
        return []
    now, ttl, out = time.time(), ttl_seconds(), []
    for path in sorted(d.glob("*.yaml")):
        if not HANDLE_RE.match(path.stem):
            continue
        try:
            last_used = path.stat().st_mtime
        except OSError:
            continue
        if now - last_used > ttl:
            if prune:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        out.append(SessionInfo(handle=path.stem, conn=_read(path), last_used=last_used, path=path))
    return sorted(out, key=lambda s: s.last_used, reverse=True)


def current() -> Optional[Connection]:
    """本对话指名的会话（--session / GSDB_SESSION）；没指名就是 None——不自动选、不列候选、不认旧文件。"""
    list_sessions()                         # 只为顺手清掉过期会话,结果一概不用
    handle = selected()
    if not handle:
        return None
    path = path_for(handle)
    if not path.exists():
        raise ConfigError(
            "会话句柄 %s 不存在或已过期（闲置超过 %d 小时会自动清理）。"
            "重新运行 gaussdb-login 登录，并带上它输出的新句柄。"
            % (handle, ttl_seconds() // 3600))
    conn = _read(path)
    _touch(path)
    return conn


def current_handle() -> Optional[str]:
    """本对话指名的句柄;没指名就是 None。"""
    return selected() or None


def clear(handle: Optional[str] = None) -> bool:
    """退出一个会话。返回是否真的删了。不指名一律拒绝——不指名退掉「唯一那个」,退的可能是别人的。"""
    handle = handle or selected()
    if not handle:
        raise ConfigError("退出会话要带 `--session <句柄>`（登录时输出的那一串）；运维清空沙箱里全部会话用 `--all`。")
    path = path_for(handle)
    if path.exists():
        path.unlink()
        return True
    return False


def clear_all() -> int:
    """清掉沙箱里全部会话（含旧的单文件），返回删掉的个数。"""
    n = 0
    for s in list_sessions(prune=False):
        s.path.unlink()
        n += 1
    legacy = _legacy_path()
    if legacy.exists():
        legacy.unlink()
        n += 1
    return n
