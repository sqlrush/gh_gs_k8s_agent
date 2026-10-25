"""Tunable deterministic-finding thresholds — port of thresholds.go.

Durations are stored as integer seconds (Go used time.Duration). go_duration()
reproduces Go's time.Duration.String() so the threshold display column matches
the original report verbatim (e.g. ">5m0s", ">2h0m0s", ">30s").
"""
from __future__ import annotations

from dataclasses import dataclass

_MIN = 60
_HOUR = 3600


def go_duration(total_seconds: int) -> str:
    """Format seconds like Go's time.Duration.String() for the values we use."""
    s = int(total_seconds)
    if s == 0:
        return "0s"
    h, rem = divmod(s, _HOUR)
    m, sec = divmod(rem, _MIN)
    out = ""
    if h:
        out += f"{h}h"
    if h or m:
        out += f"{m}m"
    out += f"{sec}s"
    return out


@dataclass(frozen=True)
class Thresholds:
    conn_pct_notice: float = 80
    conn_pct_warn: float = 90
    cache_hit_notice: float = 99
    cache_hit_warn: float = 95
    long_xact_notice: int = 5 * _MIN
    long_xact_warn: int = 30 * _MIN
    long_xact_crit: int = 2 * _HOUR
    idle_xact_notice: int = 5 * _MIN
    idle_xact_warn: int = 10 * _MIN
    idle_xact_crit: int = 30 * _MIN
    dead_ratio_notice: float = 20
    dead_ratio_warn: float = 40
    dead_tup_min: int = 100000
    block_notice: int = 5
    block_warn: int = 30
    block_crit: int = 2 * _MIN
    block_chain_warn_depth: int = 2
    wait_conc_notice: float = 50
    wait_conc_warn: float = 75
    lwlock_sessions: int = 5
    ckpt_req_notice: float = 30
    ckpt_req_warn: float = 50
    slow_sql_avg_ms: int = 500
    # 慢 SQL 的 CPU/elapsed 低于此值 → 阻塞/锁/睡眠主导(非资源消耗)
    slow_sql_low_cpu_ratio: float = 0.10
    # 执行中、还没结束的 SQL 已经跑了多久。dbe_perf.statement 只记已完成的执行,
    # 一条跑了 1000 多秒还没完的 SQL 在慢 SQL 表里一行都没有(faultlab S1)。
    # 与长事务同一组档位:跑这么久的语句一样持着快照,5 分钟在 OLTP 里已经反常。
    running_sql_notice: int = 5 * _MIN
    running_sql_warn: int = 30 * _MIN
    running_sql_crit: int = 2 * _HOUR
    active_notice: int = 20
    active_warn: int = 50
    active_conc_pct: float = 60     # 活跃集中度:top query 占活跃 >%
    active_conc_floor: int = 5      # 活跃总数下限才判集中
    repl_lag_notice: int = 16 << 20  # 复制 replay 延迟字节
    repl_lag_warn: int = 64 << 20
    # 复制槽落后的 WAL 字节。槽不消费时 WAL 只增不减，直到把盘填满 ——
    # 这和上面的 replay 延迟是两件事：那个是备库追不上，这个是**槽钉住了回收**。
    # 1 GiB 关注 / 8 GiB 告警：按常见 16 MB 一个段算，8 GiB 已经是 500 多个段。
    slot_lag_notice: int = 1 << 30
    slot_lag_warn: int = 8 << 30
    # 线程池用量百分比。开了线程池时真正的并发上限是池大小，不是 max_connections；
    # 池满后新请求排队，现场表现是「库没响应」而连接数维度一片绿。
    thread_pool_notice: float = 70
    thread_pool_warn: float = 85
    # 单个库/表空间占用多少算该说一声。**这不是「盘快满了」** ——
    # 剩余空间在这个内核上取不到（pg_stat_file 要 initial account，实测被拒），
    # 所以只能按绝对量提示「这个库很大，注意它所在的盘」，不能下「水位告警」的结论。
    storage_notice: int = 200 << 30   # 200 GiB
    storage_warn: int = 500 << 30     # 500 GiB
    index_unused_bytes: int = 10 << 20  # 索引大小下限（小索引删了不值当）
    # idx_scan=0 至少要观测多久才算证据。统计重置或实例重启后窗口很短，
    # 此时全库索引都是 0 —— 不设这道门，报告会把整库索引点名建议删掉。
    # 3 天是个工程判断：短于此的窗口盖不住日/周级的批量作业。
    index_unused_min_window_s: int = 3 * 86400
    stale_min_rows: int = 10000     # 统计新鲜度只看 >N 行的表
    # 或实时页数 >N 的表(8K 页,128 页 = 1 MB)。从没 ANALYZE 过的表冻结行数是 0,
    # 只按行数开门它永远进不了候选(faultlab V7);页数取自文件大小,不靠统计收集器。
    stale_min_pages: int = 128
    # 冻结页数与实时页数偏离多少算陈旧。判据用页数而不是 last_analyze：
    # 后者会被 pg_stat_reset() 清掉，而 ANALYZE 的成果存在 pg_statistic 里
    # 不受影响 —— 拿它当判据会把统计完好的表判成「从未分析」。
    stale_page_drift: float = 0.10
    rollback_pct: float = 10        # 回滚率 >%
    rollback_floor: int = 1000      # 事务总数下限


def default_thresholds() -> Thresholds:
    return Thresholds()
