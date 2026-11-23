"""DDL 引起的锁等待(2026-11-05):等锁的 DDL 后面排着的业务会话要补回来,DDL 持锁 / 等锁各出一条发现。

场景:会话 100 的长事务读着 fl.acct(AccessShareLock,已授予);会话 200 执行 ALTER TABLE 要 AccessExclusiveLock,
在等 100;会话 301、302 的业务查询要 AccessShareLock —— 与 100 不冲突,但排在 200 后面。lockwait.pairs 只把等待者
与已授予的持锁者配对,301、302 与 100 的组合会被当成旁观者滤掉。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-lockwait" / "scripts"))

import lockwait  # noqa: E402
from common import guc_policy  # noqa: E402

TAG = "16384:24576:0:0:0:0"


def _row(w, wmode, wait, wq, h=100, hmode="AccessShareLock", app="pay-svc"):
    return {"waiter_pid": str(w), "waiter_sessionid": str(w), "waiter_mode": wmode, "holder_pid": str(h),
            "holder_sessionid": str(h), "holder_mode": hmode, "locktype": "relation", "lock_object": "fl.acct",
            "locktag": TAG, "waiter_wait_s": str(wait), "waiter_user": "app", "waiter_app": app,
            "waiter_query": wq, "holder_state": "idle in transaction", "holder_user": "rpt",
            "holder_app": "report", "holder_xact_age_s": "900", "holder_query": "SELECT count(*) FROM fl.acct"}


RAW = [
    _row(200, "AccessExclusiveLock", 120, "ALTER TABLE fl.acct ADD COLUMN memo text", app="dba-tool"),
    _row(301, "AccessShareLock", 90, "SELECT bal FROM fl.acct WHERE id = 1"),
    _row(302, "AccessShareLock", 30, "SELECT bal FROM fl.acct WHERE id = 2"),
]


def test_business_sessions_queued_behind_a_waiting_ddl_are_kept():
    kept = lockwait._filter_conflicting(RAW)
    assert [p["waiter_sessionid"] for p in kept] == ["200"]          # 301/302 与持锁者 100 不冲突
    queued = lockwait._queued_behind(RAW, kept)
    assert sorted(p["waiter_sessionid"] for p in queued) == ["301", "302"]
    assert all(p["holder_sessionid"] == "200" and p["queued_behind"] for p in queued)


def test_ddl_queue_finding_names_the_ddl_and_the_queue_and_says_cancel_first():
    pairs = lockwait._filter_conflicting(RAW)
    pairs = pairs + lockwait._queued_behind(RAW, pairs)
    fs = {f.code: f for f in lockwait._ddl_findings(pairs)}
    q = fs["LOCK_QUEUE_BEHIND_DDL"]
    assert q.value == "2" and "会话 200" in q.evidence and "pay-svc" in q.evidence and "已等 120 秒" in q.evidence
    assert "取消这条 DDL" in q.evidence and "低峰" in q.evidence
    assert not guc_policy.violations(q.evidence)                    # lockwait_timeout 按会话级写


def test_running_ddl_that_holds_the_lock_is_reported():
    raw = [_row(301, "AccessShareLock", 40, "SELECT 1 FROM fl.acct", h=200, hmode="AccessExclusiveLock")]
    raw[0]["holder_query"] = "TRUNCATE fl.acct"
    pairs = lockwait._filter_conflicting(raw)
    fs = {f.code: f for f in lockwait._ddl_findings(pairs)}
    assert "LOCK_DDL_HOLDER" in fs and "会话 200" in fs["LOCK_DDL_HOLDER"].evidence


def test_no_queue_when_nothing_conflicts_ahead():
    raw = [_row(301, "AccessShareLock", 90, "SELECT 1"), _row(302, "AccessShareLock", 30, "SELECT 2")]
    assert lockwait._queued_behind(raw, lockwait._filter_conflicting(raw)) == []


def test_is_ddl():
    for q in ("ALTER TABLE t ADD c int", " truncate t", "CREATE UNIQUE INDEX i ON t(c)", "VACUUM FULL t", "LOCK TABLE t"):
        assert lockwait.is_ddl(q), q
    for q in ("SELECT 1", "UPDATE t SET c=1", "VACUUM t", ""):
        assert not lockwait.is_ddl(q), q


def test_markdown_has_a_ddl_section_before_recovery():
    class _R:
        def run(self, script, values=None):
            if script == lockwait.PAIRS_SCRIPT:
                return RAW
            if script == lockwait.CHAIN_SCRIPT:
                return [{"sessionid": "200", "block_sessionid": "100"}, {"sessionid": "301", "block_sessionid": "200"},
                        {"sessionid": "302", "block_sessionid": "200"}]
            return []
    md = lockwait.render_markdown(lockwait.collect(_R(), 20))
    assert "## DDL 引起的锁等待" in md and md.index("## DDL 引起的锁等待") < md.index("## 快速恢复操作")
    assert "本身也在等锁" in md
