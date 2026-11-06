"""System-SQL policy gate for sqltune.

策略:GaussDB/openGauss 系统表/系统视图上的慢 SQL 不做调优——系统对象的
结构与访问路径由内核维护,用户不能也不应在其上建索引或改写内核/监控查询;
此类慢通常反映采集频率或系统压力,不是 SQL 本身的问题。

判定必须**保守**:只有 SQL 引用的全部关系都能确定是系统对象时才判定为
系统 SQL。误放行只是多出一份无害的分析;误拦截会吞掉用户的真实调优请求,
所以拿不准一律放行(如用户 schema 下恰好叫 pg_* 的表)。
"""
from __future__ import annotations

from dataclasses import dataclass

import evidence

# 哪些表引用算系统对象:收在 common/syssql.py,与慢 SQL / Top SQL 排行排除系统 SQL 用同一份口径。
# schema 限定的引用只按 common/sysobjects.py 那张表判(名单与 proctune 共用);未限定的认 pg_ / gs_ 前缀
# 与 dual / sys_dummy / statement_history。
from common.syssql import is_system_relation as _is_system_object  # noqa: E402


@dataclass(frozen=True)
class SystemVerdict:
    is_system: bool
    system_objects: list[str]


class SystemSQLSkipped(Exception):
    """Raised by the tuning pipeline when the target SQL is system-only."""

    def __init__(self, objects: list[str]):
        super().__init__("system SQL — tuning skipped by policy")
        self.objects = objects


def system_verdict(sql_text: str) -> SystemVerdict:
    """System iff every referenced relation is a recognized system object."""
    refs = evidence.extract_table_refs(sql_text)
    hits = [r for r in refs if _is_system_object(r)]
    return SystemVerdict(is_system=bool(refs) and len(hits) == len(refs),
                         system_objects=hits)


def skip_report(objects: list[str]) -> str:
    lines = "\n".join(f"- `{o}`" for o in objects)
    return f"""# SQL Tune — 系统对象 SQL,按策略跳过

该 SQL 只访问 GaussDB/openGauss 系统表/系统视图:

{lines}

按既定策略,系统对象上的慢 SQL **不做调优**:

- 系统表/系统视图的结构与访问路径由内核维护,用户侧不能也不应在其上建索引、改写内核/监控查询;
- 此类 SQL 变慢通常反映监控采集频率过高或系统整体压力,应从采集来源与系统负载入手,而不是调这条 SQL。

未采集证据,未生成任何优化建议。这是确定性结论——对同一条 SQL 重试不会得到不同结果。
"""


def skip_json(objects: list[str]) -> dict:
    return {"skipped": True, "reason": "system-sql", "system_objects": objects}
