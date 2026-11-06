"""执行人标识。

容器部署下每个 Pod 只服务一个人,平台把工号放进 GSDB_USER_ID;报告与 findings 带上它,
审计才能落到人。单机沙箱没有这个变量,所有输出保持原样。
"""
from __future__ import annotations

import os

ENV_USER_ID = "GSDB_USER_ID"


def actor_id() -> str:
    return (os.environ.get(ENV_USER_ID) or "").strip()
