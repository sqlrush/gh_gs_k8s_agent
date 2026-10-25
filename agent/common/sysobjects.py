"""数据库自带对象的识别 —— sqltune 与 proctune 共用。

策略(用户 2026-09-21 定):这两个 skill 只调优**用户自己的** SQL 与存储过程;
GaussDB/openGauss 自带的一律忽略。系统对象的结构与访问路径由内核维护,用户既不能
也不应在其上建索引或改写内核/监控查询;这类对象上的慢通常反映采集频率或系统压力,
不是对象本身的问题。

名单从真实实例取,不是拍脑袋:
    select nspname, oid from pg_namespace where oid < 16384
在 og5(openGauss-lite 5.0.3)与 og7(7.0.0-RC1)各取一遍求并集,再补上商用 GaussDB
才有的几个。两个版本 information_schema 的 oid 不同(13728 / 14975),所以 oid 本身
不能写死,但「< 16384 = 初始化时就有」这条界线两边都成立。

**为什么按名字判而不是按 oid 判**,两处原因:
① `public` 的 oid 是 2200,也小于 16384,但它正是用户建对象的地方 —— 一刀切会把
   用户的东西全忽略,而且是静默忽略,用户只看到「按策略跳过」。
② proctune 拿到的是中间件已注册脚本返回的 `nspname`,报文里**没有 oid**;要用 oid
   就得改脚本让客户重新登记,代价远大于收益。

判定**保守**:拿不准一律当用户对象。误放行只是多出一份无害的分析;误拦截会吞掉
用户真实的调优请求,而且用户很难意识到发生了什么。
"""
from __future__ import annotations

from typing import Optional

# og5 ∪ og7 实测(oid < 16384,不含 public)+ 商用 GaussDB 才有的几个。
# 改这张表前先在目标实例上跑一遍上面那句 SQL,别凭印象加减。
SYSTEM_SCHEMAS = frozenset({
    # 两个测试实例上实测存在的
    "blockchain", "coverage", "cstore", "db4ai", "dbe_perf", "dbe_pldebugger",
    "dbe_pldeveloper", "dbe_sql_util", "information_schema", "pg_catalog",
    "pg_toast", "pkg_service", "snapshot", "sqladvisor", "xmltype",
    # 商用 GaussDB / A 兼容模式下才出现,两个实例上没有但要留着
    "dbe_application_info", "dbe_file", "dbe_lob", "dbe_match", "dbe_output",
    "dbe_random", "dbe_raw", "dbe_scheduler", "dbe_session", "dbe_task",
    "dbe_utility", "pkg_util", "pmk", "sys",
})

# 会话临时 schema 由内核按会话编号建(pg_temp_3 / pg_toast_temp_3),编号不固定,
# 进不了名单,只能按前缀认。
_SYSTEM_SCHEMA_PREFIXES = ("pg_temp_", "pg_toast_temp_")


def is_system_schema(name: Optional[str]) -> bool:
    """这个 schema 是数据库自带的吗。取不到名字时返回 False(保守方向是放行)。"""
    if not name:
        return False
    norm = name.strip().lower()
    if not norm:
        return False
    return norm in SYSTEM_SCHEMAS or norm.startswith(_SYSTEM_SCHEMA_PREFIXES)
