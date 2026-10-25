"""dataIp → 本机连接名的映射。

客户侧 GRMP 用 dataIp 找到目标高斯实例；本机没有那套实例注册表，
用一份显式映射代替：dataIp 是键，值是 ~/.gdaa/config.yaml 里的连接名。

映射文件默认不入库（内容可能含客户测试环境的真实 IP），
仓库里只提供 instances.example.yaml。
"""
from __future__ import annotations

import pathlib
from typing import Dict, Optional

import yaml


class InstanceMap:
    """不可变的 dataIp → 连接名映射。"""

    def __init__(self, mapping: Dict[str, str]):
        self._mapping = dict(mapping or {})

    def resolve(self, data_ip: str) -> Optional[str]:
        """查不到返回 None —— 由调用方转成客户那句「查不到对应高斯实例信息」。

        文档明确 dataIp 是「单 IP」，所以逗号分隔的多 IP 一律视为查不到，
        而不是取第一个：取第一个会让「传了两个实例」静默变成「只查了一个」。
        """
        if not isinstance(data_ip, str) or not data_ip:
            return None
        if "," in data_ip or " " in data_ip.strip():
            return None
        return self._mapping.get(data_ip.strip())

    def count(self) -> int:
        return len(self._mapping)

    def items(self):
        return sorted(self._mapping.items())


def load(path: pathlib.Path) -> InstanceMap:
    """读映射文件。文件不存在时返回空映射。

    空映射不是错误：此时每个 dataIp 都会得到「查不到实例」——
    与客户环境里查一个不存在的实例表现完全一致，是可预期的行为。
    启动横幅会打印已映射的实例数，避免「以为配了其实没配」。
    """
    path = pathlib.Path(path)
    if not path.exists():
        return InstanceMap({})
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("%s: 实例映射必须是 dataIp: 连接名 的映射" % path)
    return InstanceMap({str(k): str(v) for k, v in raw.items()})
