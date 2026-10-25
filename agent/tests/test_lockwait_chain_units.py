"""阻塞链上溯。**含环必须终止** —— 死锁就是链上有环，而死锁正是最该报的情形。"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-lockwait" / "scripts"))

from chain import blocked_by_root, cycles, depth, roots  # noqa: E402


def test_single_edge():
    assert roots([(2, 1)]) == {2: 1}
    assert depth([(2, 1)], 2) == 1


def test_three_level_chain_finds_the_real_root():
    """3 等 2、2 等 1 —— 3 的根是 1，不是 2。杀 2 不解堵。"""
    edges = [(3, 2), (2, 1)]
    assert roots(edges) == {3: 1, 2: 1}
    assert depth(edges, 3) == 2


def test_two_waiters_on_one_root():
    edges = [(2, 1), (3, 1)]
    assert blocked_by_root(edges) == {1: [2, 3]}


def test_fan_in_through_a_middle_node():
    """4 等 3、5 等 3、3 等 1 —— 根都是 1，且 1 最终挡住 3/4/5。"""
    edges = [(4, 3), (5, 3), (3, 1)]
    assert roots(edges) == {4: 1, 5: 1, 3: 1}
    assert sorted(blocked_by_root(edges)[1]) == [3, 4, 5]


def test_two_node_cycle_terminates():
    """**这条是本模块存在的理由。** 1 等 2、2 等 1，朴素上溯会死循环。"""
    edges = [(1, 2), (2, 1)]
    found = cycles(edges)
    assert found, "没检测到环"
    assert sorted(found[0]) == [1, 2]


def test_three_node_cycle_terminates():
    edges = [(1, 2), (2, 3), (3, 1)]
    assert sorted(cycles(edges)[0]) == [1, 2, 3]


def test_roots_does_not_hang_on_a_cycle():
    """环里的节点没有根 —— 返回它自己，且必须**返回**，不能挂住。"""
    edges = [(1, 2), (2, 1)]
    r = roots(edges)
    assert set(r) == {1, 2}


def test_chain_with_a_tail_into_a_cycle():
    """3 等 1，而 1 与 2 互相等。3 的上溯会走进环里，同样不能挂。"""
    edges = [(3, 1), (1, 2), (2, 1)]
    r = roots(edges)
    assert 3 in r
    assert cycles(edges)


def test_empty_input():
    assert roots([]) == {}
    assert cycles([]) == []
    assert blocked_by_root([]) == {}


def test_depth_of_an_unknown_session_is_zero():
    assert depth([(2, 1)], 99) == 0
