"""health 新增的四项容量/可用性判定（纯函数式，用假 runner，不连库）。

四项各自守着一种**现在完全没有信号**的故障：

  · 同步提交无同步备 —— 主库提交会一直等下去，写入全部挂住；
  · 复制槽未活跃/落后 —— WAL 只增不减，直到把盘填满；
  · 线程池用满     —— 新请求排队，现场表现「库没响应」，而连接数看着离
                      max_connections 还很远，连接维度一片绿；
  · 库/表空间占用  —— 只是占用量，**不是**「盘快满了」。

**其中最该钉死的是「单机库上不许乱报同步提交」。** og5 实测
`synchronous_commit=on` + `synchronous_standby_names='*'` + 零个备库，按
PostgreSQL 的语义该无限等待，但 og5 写入完全正常 —— openGauss 的同步复制要靠
`replconninfo` 定义对端才生效。只看那两个 GUC 就报警，会在**每一个单机库**上
乱报，而单机库是客户环境里最多的一类。虚报一次，真配错时就没人看这条了。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"

for _m in ("collectors", "model", "thresholds", "util", "report", "aggregate", "counters"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors as C                     # noqa: E402
import thresholds as TH                    # noqa: E402
from model import DIM_CONN, DIM_LOGS, DIM_REPL, DimResult  # noqa: E402


class _Runner:
    """只回答登记过的脚本；没登记的抛 QueryError —— 模拟现场还没重导白名单。"""

    def __init__(self, **tables):
        self._t = tables

    def run(self, script, values=None):
        if script in self._t:
            return self._t[script]
        from common import access
        raise access.QueryError("脚本 %s 未登记" % script)


def _th():
    return TH.default_thresholds()


def _kv(**kw):
    return [{"item": k, "value": str(v)} for k, v in kw.items()]


# ================================================================ 同步提交

def _repl(runner, repl_rows=()):
    d = DimResult(dimension=DIM_REPL, available=True, headers=[])
    note = C._sync_commit(runner, d, list(repl_rows))
    return d, note


def test_a_standalone_database_is_reported_as_not_applicable_not_healthy():
    """**本组最重要的一条。** og5 的真实配置：要求同步确认、零个备库，
    但 replconninfo 没配 → 单机，同步提交根本不生效。

    这里必须明说「不适用」而不是沉默 —— 沉默会让人以为「同步复制这块已经
    检查过并且没问题」。
    """
    r = _Runner(**{"health.sync_commit": _kv(peers_configured=0, in_recovery="false",
                                             sync_commit="on", standby_names="*")})
    d, note = _repl(r)
    assert d.findings == [], "单机库上一条发现都不该报"
    assert "单机" in note and "不适用" in note


def test_the_config_facts_never_land_in_the_standby_table():
    """**自测时真渲染错过:** 这一维的表是「每行一个备库」，表头五列
    (standby/client/state/sync/replay_lag)。往里塞一行 ["synchronous_commit","on"]
    会被补成五格、标签落在 standby 列下，读起来像有个叫 synchronous_commit 的备库。
    配置事实只进抬头，不进表。
    """
    r = _Runner(**{"health.sync_commit": _kv(peers_configured=0, in_recovery="false",
                                             sync_commit="on", standby_names="*")})
    d, note = _repl(r)
    assert d.rows == []
    assert "synchronous_commit=on" in note


def test_a_cluster_requiring_sync_with_no_sync_standby_is_critical():
    """配了对端、要求同步确认、却一个 sync 备都没有 —— 主库提交会一直等。"""
    r = _Runner(**{"health.sync_commit": _kv(peers_configured=2, in_recovery="false",
                                             sync_commit="on", standby_names="*")})
    d, note = _repl(r, repl_rows=[{"sync_state": "async"}])
    codes = [f.code for f in d.findings]
    assert codes == ["SYNC_COMMIT_NO_STANDBY"]
    assert d.findings[0].severity.name == "CRITICAL"
    assert "一直等" in d.findings[0].evidence


def test_a_cluster_with_a_sync_standby_online_is_fine():
    r = _Runner(**{"health.sync_commit": _kv(peers_configured=2, in_recovery="false",
                                             sync_commit="on", standby_names="*")})
    d, note = _repl(r, repl_rows=[{"sync_state": "Sync"}])
    assert d.findings == [] and "1 个同步备" in note


@pytest.mark.parametrize("commit", ["local", "off", ""])
def test_async_commit_never_waits_so_it_is_not_a_risk(commit):
    r = _Runner(**{"health.sync_commit": _kv(peers_configured=2, in_recovery="false",
                                             sync_commit=commit, standby_names="*")})
    d, note = _repl(r)
    assert d.findings == [] and "异步" in note


def test_an_empty_standby_names_means_no_sync_requirement():
    r = _Runner(**{"health.sync_commit": _kv(peers_configured=2, in_recovery="false",
                                             sync_commit="on", standby_names="")})
    assert _repl(r)[0].findings == []


def test_on_a_standby_the_risk_belongs_to_the_primary():
    r = _Runner(**{"health.sync_commit": _kv(peers_configured=2, in_recovery="true",
                                             sync_commit="on", standby_names="*")})
    d, note = _repl(r)
    assert d.findings == [] and "备库" in note


def test_an_unregistered_script_is_silent_not_a_crash():
    """现场还没重导白名单时，这一项静默跳过 —— 不能让整次巡检失败。"""
    d, note = _repl(_Runner())
    assert d.findings == [] and note == ""


# ================================================================ 复制槽

def _logs(runner):
    d = DimResult(dimension=DIM_LOGS, available=True, headers=[])
    n = C._wal_slots(runner, _th(), d)
    return d, n


def _slot(name="s1", active="t", lag=0):
    return {"slot_name": name, "slot_type": "physical", "database": "",
            "active": active, "lag_bytes": str(lag)}


def test_no_slots_at_all_reports_nothing():
    """og5 这种单机库一个槽都没有 —— 零行是「没有槽」，不是取数失败。"""
    d, n = _logs(_Runner(**{"health.wal_slots": []}))
    assert d.findings == [] and n == 0


def test_an_inactive_slot_is_a_warning_regardless_of_how_little_it_lags():
    """未活跃的槽最危险：消费端已经没了，槽却还钉着 WAL 回收，
    而且没有任何东西会自动清掉它。刚建的 inactive 槽落后很少，但它会一直涨 ——
    所以这一条**不看落后量**。"""
    d, _ = _logs(_Runner(**{"health.wal_slots": [_slot(active="f", lag=1024)]}))
    codes = [f.code for f in d.findings]
    assert codes == ["WAL_SLOT_INACTIVE"]
    assert "填满磁盘" in d.findings[0].evidence


def test_an_active_slot_lagging_a_lot_is_graded_by_size():
    th = _th()
    small = _logs(_Runner(**{"health.wal_slots": [_slot(lag=th.slot_lag_notice + 1)]}))[0]
    big = _logs(_Runner(**{"health.wal_slots": [_slot(lag=th.slot_lag_warn + 1)]}))[0]
    assert [f.severity.name for f in small.findings] == ["NOTICE"]
    assert [f.severity.name for f in big.findings] == ["WARN"]


def test_an_active_slot_keeping_up_is_not_reported():
    d, _ = _logs(_Runner(**{"health.wal_slots": [_slot(lag=1024)]}))
    assert d.findings == []


def test_the_slot_evidence_distinguishes_itself_from_replay_lag():
    """REPL_LAG 是「备库追不上」，这条是「槽钉住了 WAL 回收」——
    两件事混在一起，处置方向就错了。"""
    d, _ = _logs(_Runner(**{"health.wal_slots": [_slot(lag=_th().slot_lag_warn + 1)]}))
    assert "两件事" in d.findings[0].evidence


def test_unregistered_slot_script_is_silent():
    d, n = _logs(_Runner())
    assert d.findings == [] and n == 0


# ================================================================ 线程池

def _conn(runner):
    d = DimResult(dimension=DIM_CONN, available=True, headers=[])
    C._thread_pool(runner, _th(), d)
    return d


def test_the_denominator_is_the_pool_size_not_max_connections():
    """**og5 实测**：max_connections=1000、thread_pool_attr='800, 2, (nobind)'。
    先碰到的是 800。用 1000 做分母，占比会偏低 20%，刚好可能跨过告警线。"""
    r = _Runner(**{"health.thread_pool": _kv(
        enable_thread_pool="on", thread_pool_attr="800, 2, (nobind)",
        max_connections="1000", real_sessions=700)})
    d = _conn(r)
    assert [f.code for f in d.findings] == ["THREAD_POOL_HIGH"]
    assert "700/800" in d.findings[0].value       # 87.5%，不是 70%


def test_a_pool_well_under_the_line_is_not_reported():
    r = _Runner(**{"health.thread_pool": _kv(
        enable_thread_pool="on", thread_pool_attr="800, 2, (nobind)",
        max_connections="1000", real_sessions=12)})     # og5 实测 12/800
    assert _conn(r).findings == []


def test_a_pool_that_is_off_says_so_and_judges_nothing():
    r = _Runner(**{"health.thread_pool": _kv(
        enable_thread_pool="off", thread_pool_attr="", max_connections="1000",
        real_sessions=900)})
    d = _conn(r)
    assert d.findings == []
    assert any("未开启" in str(row) for row in d.rows)


@pytest.mark.parametrize("attr", ["", "坏的", "(nobind)", "0, 2", "-1, 2"])
def test_an_unparsable_pool_size_judges_nothing_and_says_why(attr):
    """解析不出池大小就不判 —— **绝不能拿 max_connections 顶上去当分母**，
    那个分母是错的，算出来的占比看着正常却毫无意义。"""
    r = _Runner(**{"health.thread_pool": _kv(
        enable_thread_pool="on", thread_pool_attr=attr,
        max_connections="1000", real_sessions=900)})
    d = _conn(r)
    assert d.findings == []
    assert any("解析不出来" in str(row) for row in d.rows)


@pytest.mark.parametrize("attr,size", [("800, 2, (nobind)", 800), ("  256 ,1", 256),
                                       ("1024", 1024)])
def test_pool_size_parsing(attr, size):
    assert C._pool_size(attr) == size


def test_the_pool_evidence_explains_the_green_connection_dimension():
    """这条发现的价值就在于解释「为什么连接数看着没问题，库却没响应」。"""
    r = _Runner(**{"health.thread_pool": _kv(
        enable_thread_pool="on", thread_pool_attr="800, 2, (nobind)",
        max_connections="1000", real_sessions=780)})
    ev = _conn(r).findings[0].evidence
    assert "排队" in ev and "max_connections" in ev


def test_the_real_session_count_comes_from_the_script_not_a_guess():
    """脚本里用 `sessionid <> 0` 过滤掉线程池的空闲工作线程（faultlab 踩过：
    不过滤会把 12 个真实会话报成几十个）。这里只验占比用的就是那个数。"""
    r = _Runner(**{"health.thread_pool": _kv(
        enable_thread_pool="on", thread_pool_attr="100, 2", max_connections="1000",
        real_sessions=86)})
    assert "86/100" in _conn(r).findings[0].value


# ================================================================ 占用量

def _storage(rows):
    return C.collect_storage(_Runner(**{"health.storage": rows}), _th(), 10)


def _s(kind, name, b):
    return {"kind": kind, "name": name, "bytes": str(b)}


def test_a_big_database_is_flagged_by_absolute_size():
    th = _th()
    d = _storage([_s("database", "postgres", th.storage_warn + 1)])
    assert [f.code for f in d.findings] == ["STORAGE_LARGE"]
    assert d.findings[0].severity.name == "WARN"


def test_a_small_database_is_listed_but_not_flagged():
    d = _storage([_s("database", "postgres", 4826251268)])   # og5 实测 4.8 GB
    assert d.findings == [] and len(d.rows) == 1


@pytest.mark.parametrize("word", ["盘快满了", "磁盘空间不足", "即将耗尽"])
def test_it_never_claims_the_disk_is_filling_up(word):
    """**剩余空间在这个内核上取不到**（pg_stat_file 要 initial account，实测被拒）。
    按一个取不到的事实下「磁盘告警」的结论，正是这套工具一直在修的那类错。

    和冻结水位那条同理：不是禁止提这个说法，而是提到时**必须是在否定它**。
    证据里那句「这不是『盘快满了』的判定」正是要留着的 —— 它在替读报告的人
    挡掉一个错误的联想。
    """
    d = _storage([_s("database", "postgres", _th().storage_warn + 1)])
    for text in (d.findings[0].evidence, d.headline):
        if word in text:
            assert "不是" in text[:text.index(word)] or "不可知" in text, \
                "提到「%s」却没有否定它：%s" % (word, text)


def test_the_evidence_says_free_space_is_unknown():
    d = _storage([_s("database", "postgres", _th().storage_warn + 1)])
    assert "取不到文件系统剩余空间" in d.findings[0].evidence
    assert "剩余空间不可知" in d.headline


def test_both_databases_and_tablespaces_are_covered():
    """表空间常挂在不同的盘上 —— 只看库大小看不出是哪块盘在被填满。"""
    d = _storage([_s("database", "postgres", 1), _s("tablespace", "pg_default", 2)])
    kinds = {row[0] for row in d.rows}
    assert kinds == {"库", "表空间"}
    assert "1 个库、1 个表空间" in d.headline


def test_an_unavailable_script_degrades_the_dimension_instead_of_crashing():
    d = C.collect_storage(_Runner(), _th(), 10)
    assert d.available is False


# ================================================================ 分区表

def _schema_dim(rows):
    d = DimResult(dimension="Schema / Objects", available=True, headers=["项", "值"])
    n = C._partitions(_Runner(**{"health.partitions": rows}), _th(), d)
    return d, n


def _p(schema="fl", table="part_log", parts=1000, strategy="r"):
    return {"schema": schema, "table": table, "parts": parts, "strategy": strategy}


def test_a_table_with_a_thousand_partitions_is_flagged():
    """og5 实测 fl.part_log 有 1000 个分区。"""
    d, n = _schema_dim([_p(parts=1000)])
    assert [f.code for f in d.findings] == ["PARTITION_COUNT_HIGH"]
    assert n == 1


def test_a_normal_partition_count_is_not_flagged_nor_listed():
    """按月分区的表(几十个分区)是正常设计 —— 报它只会把这一维刷满。"""
    d, n = _schema_dim([_p(parts=36)])
    assert d.findings == [] and d.rows == [] and n == 1


def test_a_huge_partition_count_escalates_to_warning():
    small = _schema_dim([_p(parts=C.PARTS_NOTICE)])[0]
    big = _schema_dim([_p(parts=C.PARTS_WARN)])[0]
    assert [f.severity.name for f in small.findings] == ["NOTICE"]
    assert [f.severity.name for f in big.findings] == ["WARN"]


def test_the_partition_finding_does_not_claim_anything_about_pruning():
    """**分区数正常 ≠ 分区裁剪正常。** 裁剪是执行计划的属性,要看具体 SQL 的 plan;
    一条目录查询给不出这个结论。混在一起会让人以为分区数没问题就不用查裁剪了 ——
    而最常见的分区故障恰恰是"分区数很合理,但查询没带分区键、每次全分区扫"。
    """
    ev = _schema_dim([_p(parts=2000)])[0].findings[0].evidence
    assert "不代表分区裁剪有问题" in ev and "gaussdb-explain" in ev


def test_no_partitioned_tables_reports_nothing():
    d, n = _schema_dim([])
    assert d.findings == [] and n == 0


def test_an_unregistered_partition_script_is_silent():
    d = DimResult(dimension="Schema / Objects", available=True, headers=[])
    assert C._partitions(_Runner(), _th(), d) == 0
    assert d.findings == []
