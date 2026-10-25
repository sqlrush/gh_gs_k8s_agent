"""脚本仓库：按**逻辑名**查脚本定义。

为什么是逻辑名而不是数字 ID：接口文档里 id=56 是「查看数据库信息」，
客户调用示例里同一个 id=56 却被传了慢 SQL 的参数 —— **脚本 ID 是环境
相关数据，不是稳定契约**。硬编码 ID 的失败方式极其隐蔽：换环境后 ID
依然存在，指向另一条脚本，执行成功、结果无关、不报错。
"""
from __future__ import annotations

import os
import pathlib
from typing import Dict, List, Optional

from .script import ScriptRecord, load_script

DEFAULT_SUBDIR = ("scripts", "registry")


class RegistryError(Exception):
    """脚本仓库层面的错误（找不到、重名）。"""


def default_dir() -> pathlib.Path:
    """脚本仓库位置：GRMP_REGISTRY 环境变量优先，否则取仓库内的默认目录。"""
    override = os.environ.get("GRMP_REGISTRY")
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path(__file__).resolve().parents[2].joinpath(*DEFAULT_SUBDIR)


class Registry:
    """一个脚本目录。首次访问时加载，之后进程内缓存。"""

    def __init__(self, root: Optional[pathlib.Path] = None):
        self._root = pathlib.Path(root) if root is not None else default_dir()
        self._by_name: Optional[Dict[str, ScriptRecord]] = None

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def _load(self) -> Dict[str, ScriptRecord]:
        if self._by_name is not None:
            return self._by_name
        if not self._root.is_dir():
            raise RegistryError("脚本目录不存在：%s" % self._root)
        loaded: Dict[str, ScriptRecord] = {}
        for path in sorted(self._root.rglob("*.yaml")):
            record = load_script(path)
            if record.script_name in loaded:
                raise RegistryError(
                    "逻辑名 %s 重复定义（%s）。逻辑名是跨环境的匹配键，"
                    "重名会让「按名解析 ID」变得不确定。"
                    % (record.script_name, path)
                )
            loaded[record.script_name] = record
        self._by_name = loaded
        return loaded

    def names(self) -> List[str]:
        return sorted(self._load())

    def find(self, script_name: str) -> ScriptRecord:
        loaded = self._load()
        if script_name not in loaded:
            raise RegistryError(
                "未注册脚本 %s。已注册的有：%s"
                % (script_name, ", ".join(sorted(loaded)) or "（无）")
            )
        return loaded[script_name]
