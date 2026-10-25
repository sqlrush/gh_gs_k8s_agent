"""8 级表锁的互斥矩阵。

**这张表是实测出来的，不是记出来的。** tools/probe_lock_matrix.py 在真库上
把 64 对模式逐对撞一遍（一条会话持 A、另一条请求 B，看会不会被挡），
产出的结果与本表逐格比对。换到商用 GaussDB 上重跑一遍即可知道有无差异。

拼写用 pg_locks.mode 的原样（AccessShareLock 这种），不做大小写归一 ——
归一就要在两处维护同一套别名，而报告里要显示的本来就是数据库给的那个词。
"""
from __future__ import annotations

LOCK_MODES = (
    "AccessShareLock",            # 1  SELECT
    "RowShareLock",               # 2  SELECT FOR UPDATE / FOR SHARE
    "RowExclusiveLock",           # 3  INSERT / UPDATE / DELETE
    "ShareUpdateExclusiveLock",   # 4  VACUUM(非 FULL) / ANALYZE / CREATE INDEX CONCURRENTLY
    "ShareLock",                  # 5  CREATE INDEX(非 CONCURRENTLY)
    "ShareRowExclusiveLock",      # 6  CREATE TRIGGER / 部分 ALTER TABLE
    "ExclusiveLock",              # 7  REFRESH MATERIALIZED VIEW CONCURRENTLY
    "AccessExclusiveLock",        # 8  DROP / TRUNCATE / VACUUM FULL / 多数 ALTER / LOCK TABLE 默认
)

# 每个模式与哪些模式互斥。按「由弱到强」的下标写，读起来就是标准的三角矩阵。
# 下标从 0 起，与 LOCK_MODES 对应。
_CONFLICTS = {
    0: {7},
    1: {6, 7},
    2: {4, 5, 6, 7},
    3: {3, 4, 5, 6, 7},
    4: {2, 3, 5, 6, 7},
    5: {2, 3, 4, 5, 6, 7},
    6: {1, 2, 3, 4, 5, 6, 7},
    7: {0, 1, 2, 3, 4, 5, 6, 7},
}

_INDEX = {m: i for i, m in enumerate(LOCK_MODES)}

_TYPICAL = {
    "AccessShareLock": "SELECT",
    "RowShareLock": "SELECT ... FOR UPDATE / FOR SHARE",
    "RowExclusiveLock": "INSERT / UPDATE / DELETE",
    "ShareUpdateExclusiveLock": "VACUUM（非 FULL）、ANALYZE、CREATE INDEX CONCURRENTLY",
    "ShareLock": "CREATE INDEX（非 CONCURRENTLY）",
    "ShareRowExclusiveLock": "CREATE TRIGGER、部分 ALTER TABLE",
    "ExclusiveLock": "REFRESH MATERIALIZED VIEW CONCURRENTLY",
    "AccessExclusiveLock": "DROP / TRUNCATE / VACUUM FULL / 多数 ALTER TABLE / LOCK TABLE（默认模式）",
}


def _idx(mode: str) -> int:
    """不认识的模式**抛**，不返回默认值。

    返回 False（不冲突）会让报告说「这两个模式没有互斥关系」，而现场是
    实实在在堵着的 —— 结论与事实相反，且看不出哪里错了。
    """
    try:
        return _INDEX[mode]
    except KeyError:
        raise KeyError(
            "未知锁模式 %r。已知的 8 个：%s。"
            "若数据库给出了新模式，先补进 LOCK_MODES 并用 "
            "tools/probe_lock_matrix.py 实测它与其余模式的互斥关系。"
            % (mode, ", ".join(LOCK_MODES))) from None


def conflicts(holder: str, waiter: str) -> bool:
    """holder 持有 holder 模式时，waiter 请求 waiter 模式会不会被挡。"""
    return _idx(waiter) in _CONFLICTS[_idx(holder)]


def conflict_reason(holder: str, waiter: str) -> str:
    """给报告用的一句人话。"""
    if not conflicts(holder, waiter):
        return "%s 与 %s 不互斥（本次阻塞另有原因，检查是否为行级锁）" % (holder, waiter)
    if holder == "AccessExclusiveLock":
        return ("holder 持有 %s，waiter 请求的 %s 也不例外 —— "
                "%s 与全部 8 种模式互斥，任何访问都会被挡；"
                "常见于 DROP / TRUNCATE / VACUUM FULL / ALTER TABLE"
                % (holder, waiter, holder))
    return ("holder 持有 %s，waiter 请求 %s，两者在 8 级锁矩阵中互斥"
            % (holder, waiter))


def typical_statements(mode: str) -> str:
    """哪些语句会取这个模式。报告里用来解释「它为什么会持有这把锁」。"""
    _idx(mode)   # 未知模式照样抛
    return _TYPICAL[mode]
