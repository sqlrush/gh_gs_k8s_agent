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


def default_thresholds() -> Thresholds:
    return Thresholds()
