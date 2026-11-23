"""status:另开一个终端看「现在库里哪些现象看得到」。

**它不是为了好看,是为了分清两件事:** 用 skill 查不出东西时,到底是 skill 漏报,
还是故障根本没造出来。status 只读库、不碰任何状态,给的都是能对着复查的原始数字。

**它自己不能被挡住:** 2026-10-24 全量跑时,K3 的 ALTER 排在 fl.payments 上,status 里
`pg_total_relation_size` 要拿那张表的共享锁,就排在 ALTER 后面一直挂着 —— 观测工具自己成了
锁队列的一员。现在连接带等锁 / 语句超时(OBSERVER_GUARDS),哪一项被挡住就如实写「这一项没查出来」。

**和 run 里的进度行的区别:** run 的进度行按场景自证(它知道自己起了哪些会话);
status 是从库的角度看全局 —— 等锁最久的几条、锁类型分布、等待事件排名、死元组最多的表。
就算 run 不在跑(比如只想看上一次留下的状态),它也能用。
"""
from __future__ import annotations

import time
from typing import List, Tuple

from . import config as C
from .db import OBSERVER_GUARDS, Conn

TOP_N = 6


def why_failed(exc: BaseException) -> str:
    """一项观测没查出来时给人看的一句话。等锁超时单独说清楚:多半是有 DDL 排在那张表上。"""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    low = text.lower()
    if "lock wait timeout" in low or "lockwait" in low or "could not obtain lock" in low:
        return ("等锁超过 %d 秒没等到 —— 多半有 DDL / VACUUM FULL 正排在某张表上(比如 K3 的 ALTER、"
                "V6 的 VACUUM FULL),这一项先跳过,不陪它排队" % C.OBSERVER_LOCKWAIT_S)
    if "statement timeout" in low or "canceling statement" in low:
        return "查询超过 %d 秒被取消,这一项先跳过" % C.OBSERVER_STATEMENT_S
    return text[:160]


def _rows(c: Conn, title: str, sql: str, args: Tuple = ()) -> str:
    """跑一项观测。**一项查不出来不影响别的项** —— 以前一条被挡住就整个 status 挂住。"""
    try:
        rows = c.query(sql, args)
    except Exception as exc:                 # noqa: BLE001
        c.rollback()
        return "== %s\n  (这一项没查出来:%s)" % (title, why_failed(exc))
    if not rows:
        return "== %s\n  (没有)" % title
    body = "\n".join("  " + " | ".join("-" if v is None else str(v)[:46] for v in r) for r in rows)
    return "== %s\n%s" % (title, body)


def snapshot_text() -> str:
    """一次性取全部观测。连接按观测者设超时:等锁 3 秒、单条 30 秒,不排进别人的锁队列。"""
    c = Conn("status", guards=OBSERVER_GUARDS)
    try:
        parts: List[str] = []
        parts.append(_rows(c, "实验会话(按已运行时长)",
                           "SELECT application_name, state, waiting, "
                           "       extract(epoch FROM now() - query_start)::int AS run_s, "
                           "       substr(regexp_replace(query, '\\s+', ' ', 'g'), 1, 44) "
                           "  FROM pg_stat_activity WHERE application_name LIKE %s "
                           "   AND sessionid <> 0 "
                           " ORDER BY run_s DESC NULLS LAST LIMIT 12", (C.APP_PREFIX + "%",)))
        parts.append(_rows(c, "正在等锁的(等得最久的在前)",
                           "SELECT a.application_name, "
                           "       extract(epoch FROM now() - a.query_start)::int AS wait_s, "
                           "       w.wait_status, coalesce(w.wait_event, '-'), "
                           "       coalesce(b.application_name, '(不在会话里:两阶段事务?)') AS blocker "
                           "  FROM pg_stat_activity a "
                           "  LEFT JOIN pg_thread_wait_status w ON w.sessionid = a.sessionid "
                           "  LEFT JOIN pg_stat_activity b ON b.sessionid = w.block_sessionid "
                           " WHERE a.waiting AND a.application_name LIKE %s AND a.sessionid <> 0 "
                           " ORDER BY wait_s DESC LIMIT %s", (C.APP_PREFIX + "%", TOP_N)))
        parts.append(_rows(c, "锁类型分布(已授权 / 排队)",
                           # openGauss 基于 PG 9.2,**没有 FILTER 子句**(PG 9.4 才有)——
                           # 用 CASE 求和代替。同一类坑还有 WITH ORDINALITY。
                           "SELECT locktype, mode, sum(CASE WHEN granted THEN 1 ELSE 0 END) AS held, "
                           "       sum(CASE WHEN granted THEN 0 ELSE 1 END) AS queued "
                           "  FROM pg_locks GROUP BY 1, 2 HAVING count(*) > 0 "
                           " ORDER BY 4 DESC, 3 DESC LIMIT 10"))
        parts.append(_rows(c, "等待事件排名(含轻量锁 —— 只看 pg_locks 是看不到这些的)",
                           "SELECT wait_status, coalesce(wait_event, '-'), count(*) "
                           "  FROM pg_thread_wait_status WHERE wait_status <> 'none' "
                           " GROUP BY 1, 2 ORDER BY 3 DESC LIMIT %s", (TOP_N,)))
        parts.append(_rows(c, "死元组最多的表",
                           "SELECT relname, n_live_tup, n_dead_tup, "
                           "       CASE WHEN n_live_tup + n_dead_tup > 0 THEN "
                           "         round(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 1) "
                           "         ELSE 0 END AS dead_pct, "
                           "       pg_size_pretty(pg_total_relation_size(relid)), "
                           "       to_char(last_autovacuum, 'HH24:MI:SS') "
                           "  FROM pg_stat_user_tables WHERE schemaname = %s "
                           " ORDER BY n_dead_tup DESC LIMIT %s", (C.SCHEMA, TOP_N)))
        parts.append(_rows(c, "卡住回收水位的三类东西",
                           "SELECT '长事务' AS kind, application_name, "
                           "       extract(epoch FROM now() - xact_start)::int AS age_s "
                           "  FROM pg_stat_activity "
                           " WHERE xact_start IS NOT NULL AND application_name LIKE %s "
                           "   AND now() - xact_start > interval '30 seconds' "
                           "UNION ALL SELECT '两阶段事务', gid, "
                           "       extract(epoch FROM now() - prepared)::int FROM pg_prepared_xacts "
                           "UNION ALL SELECT '复制槽', slot_name, NULL FROM pg_replication_slots "
                           " ORDER BY 3 DESC NULLS LAST", (C.APP_PREFIX + "%",)))
        parts.append(_rows(c, "线程池留下的空闲条目(sessionid=0,不持锁、没有客户端 —— 不是残留)",
                           "SELECT application_name, count(*) FROM pg_stat_activity "
                           " WHERE sessionid = 0 GROUP BY 1 ORDER BY 2 DESC LIMIT 5"))
        parts.append(_rows(c, "本库死锁累计计数(不会自己清零)",
                           "SELECT datname, deadlocks, xact_rollback, xact_commit "
                           "  FROM pg_stat_database WHERE datname = current_database()"))
        return "\n\n".join(parts)
    finally:
        c.close()


def show(watch: int = 0) -> int:
    if watch <= 0:
        print(snapshot_text())
        return 0
    if watch < 2 or watch > 600:
        print("--watch 要在 2~600 秒之间")
        return 2
    try:
        while True:
            print("\033[2J\033[H", end="")        # 清屏,原地刷新
            print("faultlab status  每 %d 秒刷新,Ctrl-C 退出\n" % watch)
            print(snapshot_text(), flush=True)
            time.sleep(watch)
    except KeyboardInterrupt:
        return 0
