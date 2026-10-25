"""health 注册脚本的 SQL 形态检查(不连库)。

这里钉的都是 2026-10-24 faultlab 在 og5 上测出来的、**跑起来不报错、只是少报**的缺陷:

  · 「真实客户端会话」按 `connection_info` 非空来认 —— psycopg2 这类走社区 libpq 的
    客户端不填这一列,它们的长事务 / 空闲事务整类看不见(K1、V2、S1);
  · 统计新鲜度按 `relpages DESC LIMIT` 取候选 —— 从没 ANALYZE 过的表 relpages=0,
    正好排在最后被截掉(V7);
  · 连接状态分布不排除线程池的空闲条目(sessionid=0)—— 总数与 idle 多算。

SQL 跑不了,只能查形态;真实效果见汇报里 og5 上的改前 / 改后对照。
"""
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_REG = _ROOT / "scripts" / "registry"

from common.grmp.script import load_script  # noqa: E402


def _sql(rel: str) -> str:
    return load_script(_REG / rel).script_content


def _flat(sql: str) -> str:
    """压成一行、全小写,便于按片段断言。"""
    return re.sub(r"\s+", " ", sql).lower()


# 认「真实客户端会话」的四处:长事务、最老事务、活跃集中度、vacuum 的阻塞源。
_CLIENT_SESSION_SCRIPTS = (
    "health/long_xact.yaml",
    "health/overview.yaml",
    "health/conn_concentration.yaml",
    "vacuum/oldest_xmin.yaml",
)


@pytest.mark.parametrize("rel", _CLIENT_SESSION_SCRIPTS)
def test_client_sessions_are_not_recognised_by_connection_info(rel):
    """psycopg2 / 社区 libpq 系驱动、不少中间件**不填** connection_info。

    og5 实测:psycopg2 会话这一列是空串,gsql(openGauss 自带的 libpq)才填
    `{"driver_name":"libpq",...}`。拿它当「是不是客户端」的判据,faultlab 里
    V2 的 1118 秒长事务、K1 的 16 分钟空闲事务全被滤掉,报告写「无活动客户端事务」。
    """
    assert "connection_info" not in _sql(rel), (
        "%s 仍按 connection_info 认客户端 —— 不填这一列的驱动整类看不见" % rel)


@pytest.mark.parametrize("rel", _CLIENT_SESSION_SCRIPTS)
def test_kernel_background_threads_stay_out(rel):
    """去掉 connection_info 之后,内核后台线程不能跟着放进来。

    og5 实测:WLM 的 `workload` 线程一直 active、带 xact_start(当初加过滤就是为了
    不把它报成 35 小时的长事务);它和 WDRSnapshot、Asp、JobScheduler 等后台线程的
    client_port 都是 NULL。客户端会话不论 TCP(端口号)还是本地 Unix socket(-1)都非空。
    """
    assert "client_port is not null" in _flat(_sql(rel)), (
        "%s 没有排除内核后台线程(client_port 为空的那些)" % rel)


@pytest.mark.parametrize("rel", ("health/long_xact.yaml", "vacuum/oldest_xmin.yaml"))
def test_the_query_never_reports_its_own_session(rel):
    """按 client_port 认客户端以后,执行这条 SELECT 的会话自己也是客户端、也 active、
    也带 xact_start —— 不排除就会被列成一条 0 秒的「客户端事务」(vacuum 那边更糟:
    R4 会把它当成阻塞源,见 oldest_xmin.yaml 的说明)。"""
    assert "pid <> pg_backend_pid()" in _flat(_sql(rel))


def test_long_xact_returns_the_columns_the_collectors_read():
    """列名是契约:xact 维度读 xact_age_s / state_age_s,执行中的长 SQL 还要 waiting
    分出「在等锁」的 —— 少一列,对应判定会静默失效(值全是空串)。"""
    sql = _flat(_sql("health/long_xact.yaml"))
    for col in ("pid", "usename", "state", "waiting", "xact_age_s", "state_age_s", "query"):
        assert re.search(r"\b%s\b" % col, sql), "long_xact 少了列 %s" % col


def test_conn_states_excludes_thread_pool_idle_entries():
    """og5 开着线程池:pg_stat_activity 里有一批 sessionid=0 的空闲工作线程条目
    (没有客户端、不持锁)。faultlab 那轮 10:02 直接查库是 61 个真会话,health 写
    「共 65：…、idle 17」—— 那 4 条全算进了总数和 idle。"""
    assert "where sessionid <> 0" in _flat(_sql("health/conn_states.yaml"))


# ---------------------------------------------------------------- 统计新鲜度候选

def _stale():
    return _flat(_sql("health/stale_stats.yaml"))


def test_stale_stats_candidates_do_not_rank_by_frozen_pages_first():
    """**V7 的根因。** 按 relpages DESC 截前 N 张:从没 ANALYZE 过的表 relpages=0,
    排在最后被截掉 —— 最该报的表恰好进不了候选。og5 上大表很多(gaussdb.bench_*),
    前 10 张全是统计完好的表。"""
    order_by = _stale().rsplit("order by", 1)[1]
    assert not order_by.strip().startswith("c.relpages desc"), order_by


def test_stale_stats_ranks_never_analyzed_tables_first():
    """排序第一键:没有统计列、或没有冻结页数基准的表 —— 这正是判定会报的两类。"""
    order_by = _stale().rsplit("order by", 1)[1]
    first_key = order_by.split(",", 1)[0]
    assert "stat_columns = 0" in first_key and "frozen_pages = 0" in first_key, order_by


def test_stale_stats_ranks_by_page_drift_before_size():
    """排序第二键:冻结页数与实时页数的偏离比例(判定用的同一个量)。按体积排在它前面,
    一张从 1 千页长到 5 千页的表会被几十张没变过的大表挤出去。"""
    order_by = _stale().rsplit("order by", 1)[1]
    keys = [k.strip() for k in order_by.split(" limit ")[0].split(",")]
    assert len(keys) >= 3 and "cur_pages" in keys[1] and "frozen_pages" in keys[1], keys
    assert keys[2].startswith("cur_pages desc"), keys


def test_stale_stats_size_gate_lets_never_analyzed_tables_in():
    """从没 ANALYZE 过的表 reltuples=0:只按 `reltuples > min_rows` 卡门槛,它连
    排序都轮不到。另一道门按**实时页数**(文件大小)开,不靠统计收集器 ——
    og5 线程池模式下,导入的会话还连着时 n_live_tup 一直是 0(实测 6 万行、n_live_tup=0),
    拿它开门等于没开。"""
    sql = _stale()
    assert "c.reltuples > {{min_rows}}" in sql
    assert "current_setting('block_size')::bigint > {{min_pages}}" in sql
    gate = sql.split(" where ", 1)[1].split(") x", 1)[0]
    assert "n_live_tup" not in gate, "候选门槛不能靠会被重置 / 未刷新的 n_live_tup"


def test_stale_stats_declares_the_page_gate_param():
    params = {p.key for p in load_script(_REG / "health/stale_stats.yaml").params}
    assert params == {"min_rows", "min_pages", "schema_filter", "limit"}


def test_overview_counts_connections_like_conn_states():
    """2026-10-24 faultlab 第二轮:Connections 维度已按 sessionid <> 0 数出 51,Overview 仍用
    sum(numbackends) 写 53 —— 线程池空闲条目也算了进去。两处同一口径。"""
    sql = _flat(_sql("health/overview.yaml"))
    assert "sum(numbackends)" not in sql
    assert "(select count(*) from pg_stat_activity where sessionid <> 0)::bigint as numbackends" in sql
