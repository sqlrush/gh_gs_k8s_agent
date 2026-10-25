"""执行中、还没结束的长 SQL(health 慢 SQL 维度的另一半,假 runner,不连库)。

**要钉死的事(faultlab S1,2026-10-24):** 一条相关子查询跑了 1123 秒、一次没跑完,
health 一句没提 —— 慢 SQL 维度只读 dbe_perf.statement,那里**只有已完成的执行**。
现在从 pg_stat_activity(复用 health.long_xact,不加白名单)看「此刻还在跑、已经跑了多久」。

下面每条测试对着一种错了看不出来的情形:等锁的语句被导去调 SQL、取数上限截掉了
最该报的那条却不说、内核系统 SQL 混进来、脚本取不到时整维度跟着丢。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"

for _m in ("collectors", "model", "thresholds", "util", "report", "aggregate", "counters",
           "running_sql"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors  # noqa: E402
import running_sql as R  # noqa: E402
import thresholds as TH  # noqa: E402
from common import access  # noqa: E402
from model import DIM_SLOWSQL, Severity  # noqa: E402


@pytest.fixture(autouse=True)
def _state_dir(monkeypatch, tmp_path):
    """系统 SQL 判定缓存落在状态目录:指到临时目录,不碰开发机上真的 GSDB_HOME。"""
    monkeypatch.setenv("GSDB_HOME", str(tmp_path / "gdaa"))


class _Runner:
    """answers: 脚本名 → 行列表 / 可调用(params → 行) / 异常实例。"""

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def run(self, script, params=None):
        self.calls.append((script, dict(params or {})))
        a = self.answers.get(script, [])
        if isinstance(a, Exception):
            raise a
        return a(params or {}) if callable(a) else a


def _act(pid, secs, query="SELECT count(*) FROM fl.orders o WHERE o.amount > (SELECT avg(amount) FROM fl.payments p WHERE p.order_id = o.id)",
         state="active", waiting="f", xact=None):
    """health.long_xact 的一行(协议下全是字符串)。"""
    return {"pid": str(pid), "usename": "fl_app", "state": state, "waiting": waiting,
            "xact_age_s": str(xact if xact is not None else secs), "state_age_s": str(secs), "query": query}


def _th():
    return TH.default_thresholds()


def test_a_statement_running_for_1123s_is_reported():
    """S1 原样:一条 active 的语句已执行 1123 秒。"""
    chk = R.judge([_act(5371, 1123.4)], _th(), top=10, fetched_cap=R.RUNNING_FETCH)
    assert [f.code for f in chk.findings] == ["SLOWSQL_RUNNING"]
    f = chk.findings[0]
    assert f.dimension == DIM_SLOWSQL and f.severity == Severity.NOTICE
    assert f.value == "1123s" and f.threshold == ">5m0s"
    assert "5371" in f.evidence and "dbe_perf.statement" in f.evidence
    assert "fl.orders" in f.evidence, "证据里要有 SQL 原文摘要,不能只给一个 pid"
    assert "pid 5371" in chk.brief and "1123s" in chk.brief


@pytest.mark.parametrize("secs,sev", [(299, None), (300, Severity.NOTICE),
                                      (1800, Severity.WARN), (7200, Severity.CRITICAL)])
def test_severity_follows_the_running_sql_bands(secs, sev):
    chk = R.judge([_act(1, secs)], _th(), top=10, fetched_cap=R.RUNNING_FETCH)
    got = [f.severity for f in chk.findings]
    assert got == ([] if sev is None else [sev])


def test_idle_in_transaction_rows_are_not_running_sql():
    """空闲事务的 state_age 是「空闲了多久」,不是「语句跑了多久」—— 那是 XACT_IDLE 的事。"""
    chk = R.judge([_act(1, 3000, state="idle in transaction")], _th(), top=10,
                  fetched_cap=R.RUNNING_FETCH)
    assert chk.findings == ()


def test_lock_waiting_statements_are_counted_but_not_sent_to_sql_tuning():
    """**等锁的语句耗时花在锁上。** faultlab 那轮 K1 链上 15 个会话等了 900 多秒,
    把它们报成「执行中的慢 SQL」会把人导去调 SQL;锁阻塞由 gaussdb-lockwait 报。
    这里只点数、指过去。"""
    rows = [_act(1, 960, waiting="t", query="UPDATE fl.acct SET bal = bal + 1 WHERE id = 1"),
            _act(2, 950, waiting="true", query="UPDATE fl.acct SET bal = bal + 1 WHERE id = 2"),
            _act(3, 1123)]
    chk = R.judge(rows, _th(), top=10, fetched_cap=R.RUNNING_FETCH)
    assert [f.metric for f in chk.findings] == ["pid 3 执行中的 SQL"]
    assert "2 条" in chk.brief and "等锁" in chk.brief


def test_longest_first_and_capped_at_top():
    rows = [_act(pid, secs) for pid, secs in ((1, 400), (2, 9000), (3, 2000), (4, 600))]
    chk = R.judge(rows, _th(), top=2, fetched_cap=R.RUNNING_FETCH)
    assert [f.metric for f in chk.findings] == ["pid 2 执行中的 SQL", "pid 3 执行中的 SQL"]
    assert "4 条" in chk.brief and "列出最长的 2 条" in chk.brief


def test_nothing_running_long_says_so():
    """检查做了、确实没有 —— 要说出来,不能和「没有这项检查」长得一样(faultlab 发现的正是后者)。"""
    chk = R.judge([_act(1, 12)], _th(), top=10, fetched_cap=R.RUNNING_FETCH)
    assert chk.findings == () and "无" in chk.brief and "5m0s" in chk.brief


def test_a_full_fetch_whose_youngest_transaction_is_old_says_it_may_have_missed_some():
    """long_xact 按事务开始时间取最老的 N 个。语句时长 ≤ 事务时长,所以只要取回的最后一行
    事务时长还没过阈值,被截掉的那些就不可能过阈值;过了,就**可能漏**,必须说。"""
    full_old = [_act(i, 10, xact=4000 - i) for i in range(5)]
    assert "可能还有" in R.judge(full_old, _th(), top=10, fetched_cap=5).brief
    full_young = [_act(i, 10, xact=100 - i) for i in range(5)]
    assert "可能还有" not in R.judge(full_young, _th(), top=10, fetched_cap=5).brief


def test_kernel_system_sql_is_excluded_and_counted():
    """与慢 SQL 表同一口径(2026-10-22 客户要求):只碰系统视图的语句不算业务慢 SQL,
    标题里写明排除了几条。"""
    rows = [_act(7, 900, query="SELECT * FROM pg_stat_activity a, pg_locks l WHERE a.pid = l.pid"),
            _act(8, 900)]
    chk = R.judge(rows, _th(), top=10, fetched_cap=R.RUNNING_FETCH)
    assert [f.metric for f in chk.findings] == ["pid 8 执行中的 SQL"]
    assert "已排除 1 条内核系统 SQL" in chk.brief


def test_an_old_whitelist_without_the_waiting_column_still_works():
    """客户还没重导白名单时 long_xact 没有 waiting 列 —— 不能 KeyError 把整维度带崩。"""
    row = _act(1, 900)
    del row["waiting"]
    assert [f.code for f in R.judge([row], _th(), top=10, fetched_cap=R.RUNNING_FETCH).findings] \
        == ["SLOWSQL_RUNNING"]


def test_check_reads_long_xact_wide_and_degrades_with_a_reason():
    r = _Runner(**{"health.long_xact": [_act(1, 900)]})
    chk = R.check(r, _th(), 10)
    assert r.calls == [("health.long_xact", {"limit": R.RUNNING_FETCH})]
    assert len(chk.findings) == 1
    bad = R.check(_Runner(**{"health.long_xact": access.QueryError("权限不足")}), _th(), 10)
    assert bad.findings == () and "取不到" in bad.brief and "权限不足" in bad.brief


# ---------------------------------------------------------------- 接进慢 SQL 维度

def _slow_row():
    return {"unique_sql_id": "3509710879", "query": "SELECT count(*) FROM fl.orders", "calls": "1",
            "avg_ms": "879586.95", "total_sec": "879.59", "cpu_sec": "550.78"}


def test_slow_sql_dimension_carries_the_running_finding_and_says_so_in_the_headline():
    r = _Runner(**{"health.slow_sql": [_slow_row()], "health.long_xact": [_act(5371, 1123)]})
    d = collectors.collect_slowsql(r, _th(), 10)
    assert [f.code for f in d.findings] == ["SLOWSQL_TOP", "SLOWSQL_RUNNING"]
    assert d.headline.startswith("Top1 avg 879587ms") and "pid 5371" in d.headline
    assert [row[0] for row in d.rows] == ["3509710879"], "执行中的语句不往已完成执行的表里塞"


def test_running_sql_is_still_checked_when_the_statement_view_is_unreadable():
    """dbe_perf.statement 取不到(权限 / 备机)不等于 pg_stat_activity 也取不到。"""
    r = _Runner(**{"health.slow_sql": access.QueryError("dbe_perf 无权限"),
                   "health.long_xact": [_act(5371, 1123)]})
    d = collectors.collect_slowsql(r, _th(), 10)
    assert d.available is False and "dbe_perf 无权限" in d.note
    assert [f.code for f in d.findings] == ["SLOWSQL_RUNNING"]
    assert "pid 5371" in d.note
