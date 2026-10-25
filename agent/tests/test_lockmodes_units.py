"""8 级锁互斥矩阵。

矩阵本身由 tools/probe_lock_matrix.py 在真库上实撞 64 对得到，
这里钉住几条**必须成立**的性质与若干实测过的具体格子。
性质比逐格断言更能抓住整表写歪：写歪一格容易，同时满足对称性和
「AccessExclusive 与一切互斥」很难。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.lockmodes import (  # noqa: E402
    LOCK_MODES, conflict_reason, conflicts, typical_statements,
)


def test_eight_modes_weakest_to_strongest():
    assert LOCK_MODES == (
        "AccessShareLock", "RowShareLock", "RowExclusiveLock",
        "ShareUpdateExclusiveLock", "ShareLock", "ShareRowExclusiveLock",
        "ExclusiveLock", "AccessExclusiveLock",
    )


def test_matrix_is_symmetric():
    """互斥是对称关系。不对称说明表抄歪了。"""
    for a in LOCK_MODES:
        for b in LOCK_MODES:
            assert conflicts(a, b) == conflicts(b, a), "%s/%s 不对称" % (a, b)


def test_access_exclusive_conflicts_with_everything():
    for m in LOCK_MODES:
        assert conflicts("AccessExclusiveLock", m)


def test_access_share_only_conflicts_with_access_exclusive():
    for m in LOCK_MODES:
        assert conflicts("AccessShareLock", m) is (m == "AccessExclusiveLock")


def test_the_pair_measured_on_og5():
    """实测过的那一对：holder AccessExclusive、waiter AccessShare，被挡住了。"""
    assert conflicts("AccessExclusiveLock", "AccessShareLock")


@pytest.mark.parametrize("a,b", [
    ("RowExclusiveLock", "RowExclusiveLock"),      # 两个 INSERT 不互斥
    ("AccessShareLock", "RowExclusiveLock"),       # SELECT 与 INSERT 不互斥
    ("RowShareLock", "RowExclusiveLock"),
])
def test_common_pairs_do_not_conflict(a, b):
    assert not conflicts(a, b)


@pytest.mark.parametrize("a,b", [
    ("ShareLock", "RowExclusiveLock"),             # 建索引挡住写
    ("ShareUpdateExclusiveLock", "ShareUpdateExclusiveLock"),  # 两个 VACUUM
    ("ExclusiveLock", "RowShareLock"),
])
def test_known_conflicting_pairs(a, b):
    assert conflicts(a, b)


def test_unknown_mode_raises_not_silently_false():
    """不认识的模式必须抛。返回 False 等于说「不冲突」——
    而实际是堵着的，报告会说「没有互斥关系」，那是最糟的形态。"""
    with pytest.raises(KeyError):
        conflicts("NoSuchLock", "AccessShareLock")


def test_reason_names_both_sides():
    r = conflict_reason("AccessExclusiveLock", "AccessShareLock")
    assert "AccessExclusiveLock" in r and "AccessShareLock" in r


def test_typical_statements_cover_all_modes():
    for m in LOCK_MODES:
        assert typical_statements(m), "%s 没有对应的典型语句说明" % m
