"""手工清理判定的阈值 —— 集中放这里，可调。

初值的依据：naptime 是 30 秒，autovac_overdue_s 取 3600（= 120 个 naptime）——
autovacuum 一轮都没轮到这张表上，才算「没跟上」，而不是「这一秒还没跑」。
"""
from dataclasses import dataclass

MB = 1024 * 1024


@dataclass(frozen=True)
class Thresholds:
    autovac_overdue_s: float = 3600.0
    dead_ratio_warn: float = 0.20
    dead_ratio_crit: float = 0.40
    min_table_bytes: int = 100 * MB
    # 持有 xmin 的事务持续多久才算「卡住回收」:任何时刻都有正常的短事务持有 xmin,不到 60 秒的是正常并发
    xmin_hold_s: float = 60.0


def default_thresholds() -> Thresholds:
    return Thresholds()
