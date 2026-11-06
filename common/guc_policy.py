"""参数建议的允许清单 —— 诊断站在应用 DBA / 运维 DBA 的角度(2026-11-05 行方要求)。

行方原话:内核指标异常多是业务、SQL、场景、存储设备引起的,优化是解决应用的异常而不是改内核逻辑,
只有非常典型的场景才允许调内核 GUC 参数(例:最大连接数、vacuum 并发数、vacuum 触发阈值)。

所以技能输出与回答里**建议修改**的参数只能是下面四类,每类有自己的写法:

  A 典型场景(实例级)   只在对应场景给方向、不给数值,写明「需运维评估」
  B 表级 / 会话级       不动实例,写在应用侧处置里,写明只对哪张表 / 哪条 SQL / 哪个会话
  C 可观测性开关         只在数据确实缺失时说明「需运维开启 X 才能诊断」,不是处置
  D 配置与安全基线       配置审计发现的客观错误,写成「与基线不符,请运维核对」

清单外的参数(random_page_cost、shared_buffers、checkpoint_segments……)诊断时可以读、可以当证据,但不作为建议出现。
清单只维护这一份:安全红线的「视角」条、gaussdb-config 的规则、回答检查(tools/answer_check.py)、
守卫测试(tests/test_guc_policy_units.py 扫描全部技能的输出与文档)都以它为准。只用标准库。
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

TYPICAL: Dict[str, str] = {        # A
    "max_connections": "连接数长期打满,且已核对是哪些应用占满了连接",
    "thread_pool_attr": "开了线程池时与 max_connections 配套",
    "autovacuum_max_workers": "autovacuum 长期追不上(待清理的表多),且已排除长事务卡住 xmin",
    "autovacuum_naptime": "同上",
    "autovacuum_vacuum_threshold": "死元组长期达不到触发线(大表)或触发过于频繁",
    "autovacuum_vacuum_scale_factor": "同上",
    "autovacuum_analyze_threshold": "统计信息长期陈旧且确认是触发阈值的问题",
    "autovacuum_analyze_scale_factor": "同上",
}
SCOPED: Dict[str, str] = {         # B
    "work_mem": "只对某条处理大量数据的 SQL 在会话级设置",
    "lockwait_timeout": "在执行 DDL 的会话上设锁等待上限",
    "autovacuum_enabled": "表级存储参数(表上关了 autovacuum 时改回)",
}
OBSERVABILITY: Dict[str, str] = {  # C
    "enable_stmt_track": "全量 SQL 采集", "track_stmt_parameter": "SQL 参数采集", "track_stmt_stat_level": "SQL 统计级别",
    "enable_wdr_snapshot": "WDR 快照", "enable_asp": "活跃会话采样", "enable_resource_track": "资源监控",
    "resource_track_level": "资源监控粒度", "enable_resource_record": "资源监控落表", "use_workload_manager": "负载管理",
    "track_functions": "函数统计", "track_activity_query_size": "SQL 文本长度", "track_activities": "活动会话采集",
    "plan_collect_thresh": "执行计划采集",
}
BASELINE: Dict[str, str] = {       # D
    "fsync": "数据可靠性", "full_page_writes": "数据可靠性", "zero_damaged_pages": "数据可靠性",
    "ignore_checksum_failure": "数据可靠性", "logging_collector": "日志",
    "archive_mode": "归档", "archive_command": "归档", "archive_dest": "归档",
    "password_encryption_type": "口令加密", "synchronous_commit": "无备机时的提交方式",
    "statement_timeout": "语句超时", "idle_in_transaction_session_timeout": "事务中空闲超时",
}
# autovacuum 总开关不在识别范围:这个词多半指清理进程本身(「autovacuum 追不上」),关掉它属配置审计的 D 类,由 gaussdb-config 自己的规则写。
# 不作为建议的参数(诊断时可以读)。这里只列出来供识别 —— 不在任何一类里的参数名同样不许建议。
NOT_ADVISED = (
    "random_page_cost", "seq_page_cost", "cpu_tuple_cost", "effective_cache_size", "shared_buffers", "wal_buffers",
    "checkpoint_segments", "checkpoint_timeout", "checkpoint_completion_target", "max_wal_size", "wal_keep_segments",
    "max_process_memory", "maintenance_work_mem", "temp_buffers", "plan_cache_mode", "from_collapse_limit",
    "join_collapse_limit", "default_statistics_target", "query_dop", "max_parallel_workers_per_gather",
    "effective_io_concurrency", "bgwriter_delay", "bgwriter_lru_maxpages", "wal_writer_delay", "commit_delay",
    "deadlock_timeout", "update_lockwait_timeout", "ddl_lockwait_timeout", "session_timeout", "enable_seqscan",
    "enable_indexscan", "enable_hashjoin", "enable_nestloop", "enable_mergejoin", "enable_bitmapscan", "enable_sort",
    "enable_hashagg", "cursor_tuple_fraction", "geqo", "geqo_threshold", "vacuum_cost_delay", "vacuum_cost_limit",
    "autovacuum_vacuum_cost_delay", "autovacuum_vacuum_cost_limit", "autovacuum_freeze_max_age",
    "vacuum_freeze_min_age", "vacuum_freeze_table_age", "max_locks_per_transaction", "max_prepared_transactions",
    "enable_thread_pool", "max_files_per_process", "standby_names", "synchronous_standby_names",
)

CATEGORY: Dict[str, str] = {}
for _cat, _names in (("A", TYPICAL), ("B", SCOPED), ("C", OBSERVABILITY), ("D", BASELINE)):
    CATEGORY.update({n: _cat for n in _names})
KNOWN = frozenset(CATEGORY) | frozenset(NOT_ADVISED)

# 每类建议必须带的写法(同一句里出现即可)
MARKERS = {
    "A": re.compile(r"运维评估|需运维|由运维|DBA 评估|表级|ALTER TABLE"),
    "B": re.compile(r"会话|表级|只对|这条 SQL|该 SQL|ALTER TABLE|单条|本条|DDL"),
    "C": re.compile(r"才能|采集|记录|看不到|取不到|缺|没有数据|无数据|监控|开启后|开启前|关着|未开启|没开"),
    "D": re.compile(r"基线|核对|配置审计|运维|安全|可靠"),
}
MARKER_TEXT = {"A": "「需运维评估」(或只在表级设置)", "B": "只对哪张表 / 哪条 SQL / 哪个会话", "C": "缺数据、需开启才能诊断",
               "D": "与基线不符、请运维核对"}

# 开启 / 关闭这类词前面是「未 / 已 / 没 / 被」时是在说状态(「未开启」「被关掉了」),不是建议
_ADVICE = re.compile(r"调大|调小|调高|调低|调到|调整|调成|调为|设为|设成|设置为|设置成|改为|改成|改到|改回|增大|加大|减小|"
                     r"提高|降低|缩小|(?<![未已没被是])(开启|打开|关闭|关掉|启用|禁用)|建议设|ALTER SYSTEM|gs_guc|"
                     r"\bSET\s+[a-z_]+\s*(=|TO)", re.I)
_NEGATION = re.compile(r"不要|禁止|严禁|不推|不建议|不能|不得|不再|不调|不改|不动|不给|无需|不必|不用|不作为|而不是|切勿|勿|"
                       r"不因|不宜|不应|不会|并非|解决不了|没用|无效")
_SENTENCE = re.compile(r"[^。;；\n!?！？]+")
_NAME = re.compile(r"(?<![A-Za-z0-9_])(%s)(?![A-Za-z0-9_])" % "|".join(sorted(map(re.escape, KNOWN), key=len, reverse=True)))


_TABLE_LEVEL = re.compile(r"表级|ALTER TABLE|这张表|该表", re.I)
_SQL_ONLY = re.compile(r"^\s*(SET|RESET|SHOW|ALTER|SELECT)\b[^\u4e00-\u9fff]*$", re.I | re.S)


def recommendations(text: str) -> List[Tuple[str, str]]:
    """text 里「建议修改某参数」的句子 → [(参数名, 句子)]。

    一句里同时有已知参数名与修改动词才算建议;带否定词(「不要调 query_dop」)的句子是禁止,不算。
    只认已知参数名 —— 表名、列名不会被误认成参数。
    """
    out: List[Tuple[str, str]] = []
    if _SQL_ONLY.match(text):            # 工具自己会话里执行的语句(如 SET statement_timeout),不是给人的建议
        return out
    for m in _SENTENCE.finditer(text):
        s = m.group(0)
        names = _NAME.findall(s)
        if not names or not _ADVICE.search(s) or _NEGATION.search(s):
            continue
        out.extend((n, s.strip()) for n in dict.fromkeys(names))
    return out


def violations(text: str) -> List[Tuple[str, str, str]]:
    """不合规的参数建议 → [(参数名, 句子, 原因)]:清单外的参数,或清单内但没按本类写法写。"""
    out: List[Tuple[str, str, str]] = []
    for name, sentence in recommendations(text):
        if name.startswith("autovacuum_") and _TABLE_LEVEL.search(sentence):
            continue                     # autovacuum 的表级存储参数:只动这张表,属 B 类(应用 DBA 的范围)
        cat = CATEGORY.get(name)
        if cat is None:
            out.append((name, sentence, "不在允许清单里(诊断可以读它,不作为建议)"))
        elif not MARKERS[cat].search(sentence):
            out.append((name, sentence, "%s 类参数要写明%s" % (cat, MARKER_TEXT[cat])))
    return out
