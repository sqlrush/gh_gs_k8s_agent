"""中间件(GRMP)接口的路径前缀 —— 部署时配置,代码里不写死。

两个接口的路径都是「前缀 + 固定尾巴」:
    查命令清单  <前缀>/common-operations
    执行脚本    <前缀>/common-operations/invoke
前缀由中间件团队给出(各家部署不同),部署时放进环境变量 GRMP_PATH_PREFIX
(容器部署在 ConfigMap agent-config 里填;非容器部署 export 到环境里)。

**没配就报错,不猜。** 用一个错的默认前缀去请求,中间件回 404,表现成「脚本不存在」或「连不上」,
排查方向会被带到中间件那边去;直接说「GRMP_PATH_PREFIX 没配」才能一眼看出是部署漏了一项。
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

PREFIX_ENV = "GRMP_PATH_PREFIX"
# 只给样例、模拟中间件与单测用;真实部署的前缀以中间件团队给的为准
EXAMPLE_PREFIX = "/example/grmp/diagnostic/agent"
LIST_SUFFIX = "/common-operations"
INVOKE_SUFFIX = LIST_SUFFIX + "/invoke"


class PrefixError(ValueError):
    """路径前缀没配或格式不对。"""


def normalize(raw: Optional[str]) -> str:
    """规整前缀:去空白与末尾的 /;空串原样返回(= 没配)。格式不对抛 PrefixError。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if not text.startswith("/"):
        raise PrefixError("%s=%r 要以 / 开头(只写路径,不写协议、地址与端口)" % (PREFIX_ENV, text))
    if any(ch.isspace() for ch in text) or "?" in text or "#" in text or "//" in text or "/../" in text + "/":
        raise PrefixError("%s=%r 不是合法的路径前缀" % (PREFIX_ENV, text))
    return text.rstrip("/")


def from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """从环境变量取前缀并规整;没配返回空串。"""
    return normalize((os.environ if env is None else env).get(PREFIX_ENV))


def require(env: Optional[Mapping[str, str]] = None) -> str:
    """取前缀,没配就抛 PrefixError(带怎么配的说明)。"""
    prefix = from_env(env)
    if not prefix:
        raise PrefixError("中间件接口路径前缀没有配置:环境变量 %s(部署时填中间件团队给的前缀,"
                          "形如 /xxx/grmp/diagnostic/agent;容器部署在 ConfigMap agent-config 里填)" % PREFIX_ENV)
    return prefix


def list_path(prefix: str) -> str:
    return prefix + LIST_SUFFIX


def invoke_path(prefix: str) -> str:
    return prefix + INVOKE_SUFFIX
