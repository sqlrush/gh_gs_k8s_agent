"""场景实现。导入本包即把全部场景注册进 base.REGISTRY。

`ALL` 只是一个「已经 import 过」的标记,给 runner 用 —— Python 里 import 一个包
不会自动加载它的子模块,少 import 一个子模块,对应的场景就会在运行时报「没有实现」。
在这里一次性列齐,比在 runner 里散着 import 可靠。
"""
from __future__ import annotations

from . import deadlock, lock, lwlock, slow, vacuum, writes   # noqa: F401
from .base import REGISTRY

ALL = tuple(sorted(REGISTRY))
