"""按锁类型解释「为什么会被挡」—— 8 级矩阵与「典型语句」只适用于表级锁(locktype=relation)。

**为什么要分开说。** pg_locks 里所有锁类型共用同一组模式名,但「哪些语句会取这个模式」只对表锁成立。
faultlab K7(2026-10-24 og5):三个会话插同一个主键,等待者在 transactionid 上等
(waiter ShareLock ← holder ExclusiveLock),报告照表锁写了「持有者取这把锁的典型语句:
REFRESH MATERIALIZED VIEW CONCURRENTLY」—— 事务号上的 ExclusiveLock 是每个事务对**自己的事务号**
持有的锁,与表锁的 ExclusiveLock 同名不同物;读的人会去找一条根本不存在的物化视图刷新。
K1 链上排队等同一行的 tuple 锁也被这么写过。

谁挡谁仍按同一张模式冲突表判(lockwait._filter_conflicting),那对所有锁类型都成立;这里只管怎么说。
"""
from __future__ import annotations

from typing import Tuple

from common.lockmodes import conflict_reason, typical_statements

_NOT_TABLE = "不是表锁，8 级表锁矩阵与「典型语句」都不适用"

# 非表级锁类型:(为什么会被挡, 持有者为什么拿着它 / 往哪查)
_EXPLAIN = {
    "tuple": ("在排队等同一行：前面已经有会话在等这一行的持有者，后来的在 tuple 锁上排队（%s）" % _NOT_TABLE,
              "根因看排在最前面的那个会话在等谁 —— 顺着下面的阻塞链往上找根"),
    "advisory": ("应用层咨询锁（pg_advisory_lock 等）：锁的是应用自己约定的编号，与表和行无关（%s）" % _NOT_TABLE,
                 "持有者不调用 pg_advisory_unlock 就一直挡着（会话级咨询锁要到会话断开才放），处置找持有它的应用"),
    "extend": ("表扩展锁：多个会话同时往同一张表追加数据页，排队扩展文件（%s）" % _NOT_TABLE,
               "通常很短；持续出现说明往这张表批量插入的并发太高，或存储写得慢"),
    "virtualxid": ("在等持有者的事务结束（等的是它的虚拟事务号，%s）" % _NOT_TABLE,
                   "常见于 CREATE INDEX CONCURRENTLY 等语句等待之前开始的事务结束；看持有者的事务为什么一直不结束"),
}


def _transactionid(waiter_query: str) -> Tuple[str, str]:
    reason = "在等持有者的事务结束（提交或回滚）—— 等在事务号上，%s" % _NOT_TABLE
    if waiter_query.lstrip().upper().startswith("INSERT"):
        detail = ("等待者在插入与持有者同一个主键 / 唯一键的值（同键插入）：要等对方提交或回滚，"
                  "才知道这个键算不算重复；持有者的事务不结束，后来的插入就一直等")
    else:
        detail = ("持有者的事务改过 / 锁住了等待者要改的那一行（UPDATE / DELETE / SELECT … FOR UPDATE），"
                  "它不结束，那一行就一直被占着；处置看持有者的事务为什么不结束")
    return reason, detail


def explain(locktype, holder_mode, waiter_mode, waiter_query: str = "") -> Tuple[str, str]:
    """(为什么互斥, 持有者为什么拿着这把锁)。

    表锁(以及没给锁类型的旧数据)照旧走 8 级矩阵,不认识的模式照旧抛 KeyError,由调用方标「互斥关系未知」。
    """
    lt = str(locktype or "").strip().lower()
    if lt in ("", "relation"):
        return (conflict_reason(holder_mode, waiter_mode),
                "持有者取这把锁的典型语句：%s" % typical_statements(holder_mode))
    if lt == "transactionid":
        return _transactionid(str(waiter_query or ""))
    if lt in _EXPLAIN:
        return _EXPLAIN[lt]
    return ("锁类型 %s，%s（waiter %s ← holder %s）" % (lt, _NOT_TABLE, waiter_mode, holder_mode),
            "看持有者正在执行的语句判断它为什么拿着这把锁")
