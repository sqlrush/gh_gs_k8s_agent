"""各 skill 共用的命令行参数：`--session <句柄>`。

17 个取数 skill 各自定义了 `-c/--conn`；句柄参数不再各写一份——漏掉一个 skill，那个 skill
就还是「猜最后登录的库」（tests/test_cli_session_units.py 按源码逐个核）。

apply 之后把句柄写进环境变量 GSDB_SESSION：health 派生的子 skill（lockwait / vacuum / waitevent）
靠继承环境拿到同一个句柄，不必逐个改 aggregate 的命令行拼装。
"""
from __future__ import annotations

import argparse
import os
import sys

from . import session
from .config import ConfigError

HELP = ("会话句柄（gaussdb-login 登录成功时输出的那一串）。同一沙箱可能有多个用户的会话，"
        "不带句柄一律按未登录拒绝，不列别人的会话；也可用环境变量 GSDB_SESSION。")


def add_session_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", default="", metavar="句柄", help=HELP)


def apply_session_arg(args: argparse.Namespace) -> None:
    """选中 --session 指名的会话；没给就什么都不动（环境变量 / 唯一会话照常生效）。

    这一行在各 skill 里紧跟 parse_args，在它们的 try/except 之外——所以形状不合法的句柄
    在这里就以退出码 2 干净地拒掉，不能让 ConfigError 变成一段 Traceback。
    """
    handle = (getattr(args, "session", "") or "").strip()
    if not handle:
        return
    try:
        session.use(handle)
    except ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        raise SystemExit(2)
    os.environ[session.ENV_HANDLE] = handle  # 子进程继承
