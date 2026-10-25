"""校准闸：用公式复算 EXPLAIN 已经给出的那个计划，逐节点与实测比对。

**这是整套推演唯一的可信度来源。**

推演的结论形如「加这条索引，代价从 263123 降到 412」。凭什么信 412？因为
同一套公式先复算了 263123 那个计划的每一个节点，并与数据库自己报的数字
对上了。对不上就是公式在这个实例上不适用 —— 当场停，不出建议。

「毫无破绽」不是推演写得长，是推演在一个**已知答案**上先被验证过。

三条纪律：

  1. **零个节点建模 = 不通过。** 不是「没有节点超差，所以通过」。空集上的
     全称命题恒真，是这类闸门最经典的失效方式。
  2. **未建模的节点不算通过，也不算失败**，单独计入覆盖率如实报出。整棵树
     因为有个 Sort 就拒绝，会让功能在真实计划上永远用不了；而假装 Sort
     也验过了，就是在骗人。
  3. **选择率从实测 Plan Rows 反推**，不用自己估。校准检验的是代价公式，
     与选择率估算解耦 —— 两处误差混在一起时，对不上就分不清是哪边错的。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from costmodel import ModelError, Variant
from plantree import PlanNode, walk

# 相对偏差容忍度。规划器的代价是确定性计算，同一套公式同样的输入应当**逐位**
# 相同；留 1% 是给浮点累积和树高估算的余量，不是给「差不多」的余量。
DEFAULT_TOLERANCE = 0.01

MATCHED = "matched"
MISMATCHED = "mismatched"
UNMODELED = "unmodeled"


@dataclass(frozen=True)
class NodeCheck:
    node_type: str
    relation: str
    measured_total: float
    computed_total: Optional[float]
    deviation: Optional[float]      # 相对偏差，未建模时为 None
    status: str
    reason: str = ""
    approximate: bool = False
    estimate: object = None         # costmodel.Estimate，报告要拿它逐项渲染


@dataclass(frozen=True)
class Calibration:
    variant: Variant
    checks: List[NodeCheck] = field(default_factory=list)
    passed: bool = False
    reason: str = ""

    @property
    def modeled(self) -> List[NodeCheck]:
        return [c for c in self.checks if c.status != UNMODELED]

    @property
    def coverage(self) -> float:
        if not self.checks:
            return 0.0
        return len(self.modeled) / float(len(self.checks))

    @property
    def worst_deviation(self) -> Optional[float]:
        devs = [c.deviation for c in self.modeled if c.deviation is not None]
        return max(devs) if devs else None

    def summary(self) -> str:
        total = len(self.checks)
        ok = sum(1 for c in self.checks if c.status == MATCHED)
        bad = sum(1 for c in self.checks if c.status == MISMATCHED)
        skipped = total - ok - bad
        worst = self.worst_deviation
        worst_text = "n/a" if worst is None else "%.4f%%" % (worst * 100.0)
        return ("复算 %d/%d 个节点：吻合 %d、超差 %d、未建模 %d；最大偏差 %s"
                % (ok + bad, total, ok, bad, skipped, worst_text))


# resolver(node, measured_children) -> Estimate | None
#   返回 None 表示「这个算子没建模」。
#   抛 ModelError 表示「建模了但这次输入不足以复算」，同样计为未建模，
#   但把原因带出来 —— 「没实现」和「实现了但缺 correlation」要分得开。
Resolver = Callable[[PlanNode, List[PlanNode]], object]


def calibrate(root: PlanNode, resolver: Resolver,
              variant: Optional[Variant] = None,
              tolerance: float = DEFAULT_TOLERANCE) -> Calibration:
    """按给定变体复算整棵树并比对。"""
    variant = variant or Variant()
    checks: List[NodeCheck] = []

    for node in walk(root):
        checks.append(_check_node(node, resolver, tolerance))

    modeled = [c for c in checks if c.status != UNMODELED]
    if not modeled:
        return Calibration(
            variant=variant, checks=checks, passed=False,
            reason="一个节点都没能复算 —— 没有任何可比对的证据。"
                   "空集上的「全部吻合」是恒真命题，不能当成校准通过。")

    bad = [c for c in modeled if c.status == MISMATCHED]
    if bad:
        worst = max(bad, key=lambda c: c.deviation or 0.0)
        return Calibration(
            variant=variant, checks=checks, passed=False,
            reason="%d 个节点复算与实测对不上，最差的是 %s（相对偏差 %.4f%%，"
                   "复算 %.2f vs 实测 %.2f）。模型在这个实例上不适用，"
                   "不出代价结论。"
                   % (len(bad), worst.node_type, (worst.deviation or 0) * 100.0,
                      worst.computed_total or 0.0, worst.measured_total))

    return Calibration(variant=variant, checks=checks, passed=True,
                       reason="全部已建模节点复算与实测吻合。")


def calibrate_best_variant(root: PlanNode, resolver_factory,
                           tolerance: float = DEFAULT_TOLERANCE) -> Calibration:
    """把已知的内核版本分歧点逐个试过去，返回能对上的那个。

    这是把「openGauss 的代价函数跟的是哪一版 PostgreSQL」从一个**假设**变成
    一次**测量**：不替它选，让真实计划来选。

    全都对不上时返回覆盖率最高、最大偏差最小的那次尝试 —— 它的 checks 里有
    逐节点的偏差，是排查从哪一项开始偏的唯一线索。
    """
    attempts = []
    for split_seq in (True, False):
        for page_cpu in (True, False):
            variant = Variant(min_io_split_seq=split_seq,
                              btree_page_cpu_cost=page_cpu)
            attempts.append(calibrate(root, resolver_factory(variant),
                                      variant, tolerance))

    for attempt in attempts:
        if attempt.passed:
            return attempt

    def rank(c):
        worst = c.worst_deviation
        return (-c.coverage, worst if worst is not None else float("inf"))

    best = sorted(attempts, key=rank)[0]
    return Calibration(
        variant=best.variant, checks=best.checks, passed=False,
        reason="四种内核版本变体都对不上。最接近的一次：%s。%s"
               % (best.summary(), best.reason))


# --- 内部 --------------------------------------------------------------------

def _check_node(node: PlanNode, resolver: Resolver,
                tolerance: float) -> NodeCheck:
    try:
        estimate = resolver(node, list(node.children))
    except ModelError as exc:
        return NodeCheck(
            node_type=node.node_type, relation=node.relation,
            measured_total=node.total_cost, computed_total=None,
            deviation=None, status=UNMODELED,
            reason="输入不足以复算：%s" % exc)

    if estimate is None:
        return NodeCheck(
            node_type=node.node_type, relation=node.relation,
            measured_total=node.total_cost, computed_total=None,
            deviation=None, status=UNMODELED,
            reason="该算子未建模")

    deviation = relative_deviation(estimate.total_cost, node.total_cost)
    status = MATCHED if deviation <= tolerance else MISMATCHED
    return NodeCheck(
        node_type=node.node_type, relation=node.relation,
        measured_total=node.total_cost, computed_total=estimate.total_cost,
        deviation=deviation, status=status,
        approximate=getattr(estimate, "approximate", False),
        estimate=estimate)


def relative_deviation(computed: float, measured: float) -> float:
    """相对偏差。

    实测为 0 时退化成绝对差 —— 除以 0 会得到 inf 或 nan，而 nan 参与任何
    比较都返回 False，`deviation <= tolerance` 会**判成不超差**，于是一个
    彻底算错的节点被当成吻合。这是本文件里最隐蔽的一个坑。
    """
    if math.isnan(computed) or math.isnan(measured):
        return float("inf")
    if measured == 0.0:
        return abs(computed)
    return abs(computed - measured) / abs(measured)
