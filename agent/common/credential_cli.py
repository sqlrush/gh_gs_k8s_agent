#!/usr/bin/env python3
"""口令的加密存取 —— 配置文件里不允许出现明文，口令一律走这里。

    python3 -m common.credential_cli set og          # 交互输入，不回显
    python3 -m common.credential_cli set og --stdin  # 从管道读（脚本用）
    python3 -m common.credential_cli list            # 有哪些连接存了口令
    python3 -m common.credential_cli check og        # 能不能解开（不打印口令）
    python3 -m common.credential_cli seal og         # 输出可内联进 yaml 的密文

存放位置 `$GSDB_HOME/credentials/<name>.enc`，AES-256-GCM，钥匙在
`$GSDB_HOME/key`。**AAD 绑定连接名** —— 密文挪到别的连接名下会解不开，
所以密文泄露了也不能被复用到另一条连接上。

**本模块永不打印口令原文。** check 只回答「能不能解开」，seal 输出的是密文。
诊断脚本会在日志、报告、聊天窗口里流转，口令一旦被打印一次就收不回来了。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import List, Optional

from .config import ConfigError, ensure_dir, state_dir
from .credential import (
    CredentialError,
    load_secret,
    save_secret,
    seal_secret,
)


def _read_secret(from_stdin: bool, name: str) -> str:
    if from_stdin:
        # 末尾换行是管道带来的，不是口令的一部分。只剥换行，不 strip ——
        # 口令**可以**以空格开头或结尾，strip 掉会存下一个错的口令，
        # 而错在哪要等到连库失败才知道。
        return sys.stdin.read().rstrip("\r\n")
    try:
        first = getpass.getpass("连接 %s 的口令（不回显）: " % name)
        again = getpass.getpass("再输一次确认: ")
    except EOFError:
        # 非交互环境（管道、CI、被脚本调用）下 getpass 直接抛 EOFError，
        # 用户看到的是一段 Python 栈。实测在 deploy.sh 被管道喂参数时踩到。
        raise ConfigError(
            "当前不是交互终端，读不到口令输入。\n"
            "在脚本/管道里请改用 --stdin：\n"
            "    printf '%%s' \"$PASSWORD\" | "
            "python3 -m common.credential_cli set %s --stdin" % name
        ) from None
    if first != again:
        raise ConfigError("两次输入不一致，未保存。")
    if not first:
        raise ConfigError("口令为空，未保存。")
    return first


def cmd_set(args) -> int:
    secret = _read_secret(args.stdin, args.name)
    save_secret(args.name, secret)
    path = state_dir() / "credentials" / ("%s.enc" % args.name)
    print("已加密存入 %s（权限 600）" % path)
    print("现在把 config.yaml 里该连接的 password / encrypted 两行删掉 —— "
          "留着明文等于白存。")
    return 0


def cmd_list(args) -> int:
    cred_dir = state_dir() / "credentials"
    if not cred_dir.exists():
        print("凭据目录还不存在：%s" % cred_dir)
        return 0
    names = sorted(p.stem for p in cred_dir.glob("*.enc"))
    if not names:
        print("凭据目录里没有已存的口令：%s" % cred_dir)
        return 0
    print("已存口令的连接（%d 个）：" % len(names))
    for n in names:
        print("  %s" % n)
    return 0


def cmd_check(args) -> int:
    """只回答能不能解开，**不打印口令**。"""
    try:
        secret = load_secret(args.name)
    except CredentialError as exc:
        print("✗ %s：%s" % (args.name, exc), file=sys.stderr)
        return 1
    print("✓ %s：可正常解密（长度 %d，内容不显示）" % (args.name, len(secret)))
    return 0


def cmd_seal(args) -> int:
    """把口令加密成可内联进 config.yaml 的 base64。

    内联密文与凭据目录同一把钥匙、同一个 AAD，两种放法可以互换。提供它是
    因为有些交付场景要求配置自包含（一个文件带走全部），但**优先用凭据目录**：
    配置文件被复制的次数远多于凭据目录。
    """
    secret = _read_secret(args.stdin, args.name)
    print(seal_secret(args.name, secret))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="credential_cli",
        description="口令的加密存取（配置文件里不允许出现明文口令）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("set", help="加密保存一条连接的口令")
    p.add_argument("name")
    p.add_argument("--stdin", action="store_true", help="从标准输入读（脚本用）")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("list", help="列出已存口令的连接")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("check", help="校验能否解密（不打印口令）")
    p.add_argument("name")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("seal", help="输出可内联进 yaml 的密文")
    p.add_argument("name")
    p.add_argument("--stdin", action="store_true")
    p.set_defaults(func=cmd_seal)

    args = ap.parse_args(argv)
    ensure_dir()
    try:
        return args.func(args)
    except (ConfigError, CredentialError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
