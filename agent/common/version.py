"""技能包版本:common/VERSION(随发布标签一起改,gateway/tests 的守卫测试盯着两者一致)。

2026-11-08 加。行方 11-06 把新的技能包下载了却没装进正在用的技能目录,现场一直跑旧版、排查了一晚上 ——
登录输出与部署自检都显示这个版本,一眼就能看出「现在跑的是哪一版技能」。
"""
from __future__ import annotations

import pathlib

_FILE = pathlib.Path(__file__).resolve().parent / "VERSION"


def package_version() -> str:
    """读不到(v1.4.10 之前的包没有这个文件)返回空串。"""
    try:
        return _FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
