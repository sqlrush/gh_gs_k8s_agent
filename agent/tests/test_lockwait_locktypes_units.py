"""「互斥关系」按锁类型解释 —— 8 级矩阵与「典型语句」只适用于表级锁(locktype=relation)。

faultlab K7(2026-10-24 og5):三个会话插同一个主键,等待者在 transactionid 上等(waiter ShareLock ←
holder ExclusiveLock)。报告把它套进了表锁矩阵,「持有者取这把锁的典型语句」写成了
REFRESH MATERIALIZED VIEW CONCURRENTLY —— 事务号上的 ExclusiveLock 是每个事务对自己的事务号
持有的锁,与表锁同名不同物。K1 链上 tuple 锁的那几对同样被这么写。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-lockwait" / "scripts"))

import locktypes  # noqa: E402
import lockwait  # noqa: E402

_TABLE_TYPICAL = "REFRESH MATERIALIZED VIEW"


def _pair(**kw):
    base = dict(waiter_pid="1002", waiter_sessionid="5581", waiter_mode="ShareLock",
                holder_pid="1001", holder_sessionid="5580", holder_mode="ExclusiveLock",
                locktype="transactionid", lock_object="", locktag="8f0ca52:0:0:0:0:6",
                waiter_wait_s="24.0", waiter_user="fl_app", waiter_app="fl:K7/race0",
                waiter_query="INSERT INTO fl.uniq_race(k, who) VALUES (777, 'race0')",
                holder_state="idle in transaction", holder_user="fl_app", holder_app="fl:K7/first",
                holder_xact_age_s="24.8", holder_query="INSERT INTO fl.uniq_race(k, who) VALUES (777, 'first')")
    base.update(kw)
    return base


class _Runner:
    def __init__(self, pairs):
        self._pairs = pairs

    def run(self, script, values=None):
        if script == "lockwait.pairs":
            return self._pairs
        if script == "lockwait.chain":
            return [{"sessionid": p["waiter_sessionid"], "block_sessionid": p["holder_sessionid"]}
                    for p in self._pairs]
        raise AssertionError("没料到的脚本 %s" % script)


def _conflict_section(pairs) -> str:
    md = lockwait.render_markdown(lockwait.collect(_Runner(pairs), limit=20))
    return md.split("## 互斥关系", 1)[1].split("## 阻塞链与根", 1)[0]


def test_same_key_insert_on_transactionid_is_explained_as_waiting_for_the_other_transaction():
    sec = _conflict_section([_pair()])
    assert "等持有者的事务结束" in sec and "同一个主键" in sec
    assert _TABLE_TYPICAL not in sec and "两者在 8 级锁矩阵中互斥" not in sec


def test_row_update_on_transactionid_names_the_row_not_a_table_lock():
    sec = _conflict_section([_pair(waiter_query="UPDATE fl.acct SET bal = bal + 1 WHERE id = 1")])
    assert "等持有者的事务结束" in sec and "那一行" in sec
    assert _TABLE_TYPICAL not in sec


def test_tuple_lock_is_queueing_on_the_same_row():
    sec = _conflict_section([_pair(locktype="tuple", waiter_mode="ExclusiveLock", lock_object="fl.acct",
                                   waiter_query="UPDATE fl.acct SET bal = bal + 1 WHERE id = 1")])
    assert "排队" in sec and "同一行" in sec and _TABLE_TYPICAL not in sec


def test_advisory_lock_is_explained_as_application_level():
    sec = _conflict_section([_pair(locktype="advisory", waiter_query="SELECT pg_advisory_lock(880001)")])
    assert "咨询锁" in sec and _TABLE_TYPICAL not in sec


def test_relation_lock_keeps_the_matrix_and_typical_statements():
    sec = _conflict_section([_pair(locktype="relation", lock_object="public.t", waiter_mode="RowExclusiveLock",
                                   holder_mode="ShareLock", waiter_query="UPDATE t SET v = 1")])
    assert "8 级锁矩阵" in sec and "典型语句" in sec and "CREATE INDEX" in sec


@pytest.mark.parametrize("locktype", ["extend", "virtualxid", "page", "object", "something_new"])
def test_other_lock_types_never_borrow_table_lock_statements(locktype):
    reason, detail = locktypes.explain(locktype, "ExclusiveLock", "ExclusiveLock")
    assert reason and detail
    assert _TABLE_TYPICAL not in reason + detail


def test_unknown_table_lock_mode_still_raises_for_the_caller_to_flag():
    """表锁模式不认识时照旧抛(渲染层标「互斥关系未知」),不能静默当成不冲突。"""
    with pytest.raises(KeyError):
        locktypes.explain("relation", "WeirdLock", "AccessShareLock")
