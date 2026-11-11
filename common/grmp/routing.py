"""切到业务库执行(2026-11-11 行方《高斯智能体调用 grmp 中间件切库问题说明》)。

中间件默认连实例的 postgres 库。脚本登记为 ``database_type = appbusiness``、``parameter_config`` 最前面声明了
``dbName``、调用接口二时 ``param`` 里带上 ``dbName`` 的值,中间件才切到该业务库执行。库名不写在白名单里,
每次调用时给,所以同一条脚本这次可以连 A 库、下次连 B 库。

哪些脚本该切库,按脚本「读的是什么」定(og5 直连实验 + GaussDB 25.1.32 官方文档核对过):
  - 读对象目录(pg_class / pg_namespace / pg_attribute / pg_index / pg_proc / pg_stats / pg_stat_user_tables、
    information_schema、ADM_* …)或要 EXPLAIN 业务 SQL 的 → 业务库(appbusiness):这些每个库各一份;
  - 读 SQL 执行历史(STATEMENT_HISTORY)、WDR 快照(snapshot.*)、活跃会话采样落表(GS_ASP)的 → 必须 postgres:
    官方文档「必须在 postgres 库内查询,其它库中不存数据」;切过去查不到;
  - 只读实例级视图(pg_stat_activity、pg_locks、dbe_perf.*、pg_settings …)的 → postgres:在哪执行结果都一样。

单一事实源是脚本仓库 YAML 里的 ``database_type``;本模块的 ``BUSINESS_DB_SCRIPTS`` 是运行时用的副本
(技能进程不必为了判一条脚本去加载全部 YAML),``tests/test_grmp_routing_units.py`` 盯着两边一致,
另有一道守卫按上面的规则核对每条脚本的划分。
"""
from __future__ import annotations

import json
from typing import Any, Dict

APPBUSINESS = "appbusiness"
POSTGRES = "postgres"
DATABASE_TYPES = (POSTGRES, APPBUSINESS)

# 路由参数:接口二 param 里的名字;parameter_config 里的声明与行方测试环境已改好的记录逐字一致
# (注意 type 是 "String",不是我们其他参数统一写的 "STRING")
PARAM = "dbName"
CONFIG_ENTRY: Dict[str, Any] = {"key": PARAM, "value": "", "type": "String", "autoAcquire": False}
CONFIG_ENTRY_JSON = json.dumps(CONFIG_ENTRY, ensure_ascii=False, separators=(",", ":"))
# 接口一(命令清单)里的参数定义:行方规范 required 为 false —— 不传时中间件连 postgres
API_PARAM: Dict[str, Any] = {"param_name": PARAM, "data_type": "String", "required": False,
                             "description": "业务库名(中间件据此切到该库执行;不传则连 postgres)"}

# 由脚本仓库生成,改了 YAML 的 database_type 要同步改这里(守卫测试会提示)
BUSINESS_DB_SCRIPTS = frozenset({
    "explain.kernel_funcs", "explain.plan_text", "explain.plan_text_analyze", "explain.plan_text_analyze_schema",
    "explain.plan_text_schema", "explain.relation_schemas",
    "health.invalid_index", "health.partitions", "health.stale_stats", "health.table_writes", "health.unused_index",
    "lockwait.pairs",
    "login.current_db",
    "memanalyze.cols_bare", "memanalyze.cols_qualified",
    "perf.locks", "perf.table_stat",
    "procinfo.locate_schema", "procinfo.locate_search", "procinfo.proc_def", "procinfo.source_adm",
    "proctune.column_stats", "proctune.indexes", "proctune.locate_schema", "proctune.locate_search",
    "proctune.plan_text", "proctune.plan_text_analyze", "proctune.plan_text_analyze_schema", "proctune.plan_text_schema",
    "proctune.proc_def", "proctune.source_adm", "proctune.tables",
    "sqlreview.indexes", "sqlreview.tables",
    "sqltune.column_stats", "sqltune.column_types", "sqltune.indexes", "sqltune.plan_json", "sqltune.plan_json_schema",
    "sqltune.plan_text", "sqltune.plan_text_analyze", "sqltune.plan_text_analyze_schema", "sqltune.plan_text_schema",
    "sqltune.stats_freshness", "sqltune.tables",
    "topproc.top_procs",
    "vacuum.bloat_estimate", "vacuum.dead_tuples",
})

# 登录时核实「是不是真的切到了业务库」用的探针(appbusiness,SELECT current_database())
PROBE = "login.current_db"


def is_business(script_name: str) -> bool:
    return script_name in BUSINESS_DB_SCRIPTS
