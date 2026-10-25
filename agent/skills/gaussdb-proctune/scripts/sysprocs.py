"""系统存储过程策略闸 —— 只调优用户自己的过程(用户 2026-09-21 定)。

与 sqltune 的 systables.py 同一套策略、同一份名单(common/sysobjects.py):
GaussDB/openGauss 自带的过程与包由内核维护,用户既不能也不应改写它们;
在它们身上做调优分析,给出的建议要么没法落地,要么落地了就是改内核对象。

判定按 **schema 名**:proctune 拿到的是中间件已注册脚本返回的 `nspname`,报文里
没有 oid —— 要用 `pg_proc.oid < 16384` 就得改脚本让客户重新登记,代价远大于收益。

判定**保守**:schema 取不到、或不在名单里,一律当用户过程放行。误放行只是多出
一份无害的分析;误拦截会吞掉用户真实的调优请求,而且用户很难意识到发生了什么。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from common import sysobjects


@dataclass(frozen=True)
class ProcVerdict:
    is_system: bool
    schema: str
    name: str
    package: str = ""

    @property
    def qualified(self) -> str:
        parts = [p for p in (self.schema, self.package, self.name) if p]
        return ".".join(parts)


class SystemProcSkipped(Exception):
    """调优流程在目标过程是系统自带时抛出;调用方按 exit 0 收尾。"""

    def __init__(self, verdict: "ProcVerdict"):
        super().__init__("system procedure — tuning skipped by policy")
        self.verdict = verdict


def verdict(schema: Optional[str], name: str, package: str = "") -> ProcVerdict:
    return ProcVerdict(is_system=sysobjects.is_system_schema(schema),
                       schema=(schema or "").strip(), name=name,
                       package=(package or "").strip())


def skip_report(v: ProcVerdict) -> str:
    return f"""# Proc Tune — 系统自带过程,按策略跳过

目标过程 `{v.qualified}` 属于 GaussDB/openGauss 自带的 schema `{v.schema}`。

按既定策略,系统自带的存储过程与包 **不做调优**:

- 其实现与访问路径由内核维护,用户既不能也不应改写;
- 在其上给出的索引或改写建议要么无法落地,要么落地即是改动内核对象;
- 这类过程变慢通常反映调用频率或系统整体压力,应从调用方与系统负载入手。

未采集证据,未生成任何优化建议。这是确定性结论——对同一个过程重试不会得到不同结果。

要调优自己的过程,请给出用户 schema 下的过程名(例如 `业务schema.过程名`)。
"""


def skip_json(v: ProcVerdict) -> dict:
    return {"skipped": True, "reason": "system-proc", "proc": v.qualified, "schema": v.schema}
