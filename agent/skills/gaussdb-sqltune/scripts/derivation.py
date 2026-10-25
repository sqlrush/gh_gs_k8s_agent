"""推演报告：把「凭什么信这些数」摊开给人看。

报告的顺序就是可信度的依赖顺序，任何一节不成立，后面的都不该出现：

    0  统计信息新鲜度   不新鲜 → 输入本身不可信，整段作废
    1  代价常数         本实例实际值，让人看出哪些被调过
    2  复算基线（校准） 用公式重算 EXPLAIN 已给出的计划，逐节点比对
    3  假设路径         只有 0 和 2 都通过才计算 —— **不通过就不算**，
                        不是「算了再标成不可信」。数字一旦印出来就会被读，
                        旁边那行免责声明拦不住。
    4  结论             基线与假设的可靠性不同，必须说破

**这份报告存在的意义不是把话说长，是让每个数字可被独立复核。** 所以每个
算子都摊开成逐项算式（代入了实际数值），每一项都能拿计算器验。

三句话必须出现在报告里，不能因为「显得不够自信」而删：
  · cost 是规划器的内部单位，不是时间
  · 未建模的算子没有参与校准，覆盖率就是「验过多少」
  · 用了估算值的节点单独标出（目前只有 btree 树高）
"""
from __future__ import annotations

from typing import Optional, Sequence

import calibrate as _cal
import render

# 报告里反复出现的免责话术。集中在这里，避免各处措辞不一致 ——
# 措辞一旦软化（「大约」「预计提升」），读的人对确定性的判断就被误导了。
COST_IS_NOT_TIME = (
    "**cost 是规划器的内部单位，不是时间。** 代价降低 N 倍不等于快 N 倍："
    "它衡量的是规划器认为要做多少工作，不含实际 IO 等待、锁、并发干扰。")


def render_report(calibration, constants, freshness: Sequence,
                  sql: str = "", plan_text: str = "",
                  proposals: Optional[Sequence] = None) -> str:
    out = ["# 代价推演\n"]
    if sql:
        out.append("## 被分析的 SQL\n\n" + render.code_block("sql", sql))

    out.append(_section_freshness(freshness))
    out.append(_section_constants(constants))
    out.append(_section_calibration(calibration))
    out.append(_section_proposals(calibration, freshness, proposals or []))
    out.append(_section_verdict(calibration, freshness, proposals or []))
    return "\n".join(out)


def may_emit_advice(calibration, freshness: Sequence):
    """能不能出代价结论。返回 (可以吗, 理由)。

    两道门都要过。**顺序不能反**：统计不新鲜时校准可能照样通过（公式没错，
    错的是喂给公式的数），那种「通过」是最危险的一种，因为它看起来最像
    验证过了。
    """
    stale = [f for f in freshness if not f.fresh]
    if stale:
        return False, ("统计信息不新鲜：%s。推演的每一个输入都来自上次 ANALYZE "
                       "的快照，快照过期时公式再对也算不出对的数。"
                       % "；".join("%s（%s）" % (f.table, f.reason) for f in stale))
    if not calibration.passed:
        return False, "复算未通过校准：%s" % calibration.reason
    return True, ("统计新鲜且复算与实测吻合（%s），可以出代价结论。"
                  % calibration.summary())


# --- 各节 --------------------------------------------------------------------

def _section_freshness(freshness: Sequence) -> str:
    out = ["## 0. 统计信息新鲜度\n",
           "推演的全部输入（页数、行数、n_distinct、correlation）都是上次 "
           "ANALYZE 时冻结的快照。表在那之后变化太大，这些数不会报错，"
           "只会让推演算出一个**精确的错数**。所以先判新鲜度。\n",
           "判据是**冻结页数 vs 实时页数**，以及 pg_stats 里有没有统计行 —— "
           "这两个信号不受 `pg_stat_reset()` 影响。`last_analyze` 那一列仅供"
           "参考：它是统计收集器的计数器，可以被单独清掉，而 ANALYZE 的成果"
           "存在 pg_statistic 里不受影响，拿它当判据会把统计完好的表误判成"
           "「从未分析」。\n"]
    if not freshness:
        out.append("> 没有拿到任何表的新鲜度数据 —— 无法判断，按不通过处理。\n")
        return "\n".join(out)

    rows = []
    for f in freshness:
        drift = "n/a" if f.drift is None else "%.1f%%" % (f.drift * 100.0)
        rows.append([f.table, "%.0f" % f.relpages, "%.0f" % f.cur_pages, drift,
                     str(f.stat_columns), f.last_analyze or "无记录",
                     "通过" if f.fresh else "**不通过**"])
    out.append(render.table(
        ["表", "冻结页数", "实时页数", "页偏离", "统计列数",
         "last_analyze（参考）", "判定"], rows))

    for f in freshness:
        if not f.fresh:
            out.append("- **%s**：%s" % (f.table, f.reason))
    return "\n".join(out) + "\n"


def _section_constants(constants) -> str:
    out = ["## 1. 代价常数（本实例实际值）\n",
           "全部读自 pg_settings，不使用任何默认值。调过参的实例上，"
           "拿出厂默认值去算会让复算与实测对不上，而对不上正是判定"
           "「模型不可信」的依据 —— 用默认值顶替等于让闸门失灵。\n"]
    if constants is None:
        out.append("> 未取到代价常数。\n")
        return "\n".join(out)
    out.append(render.table(["参数", "取值"],
                            [[k, v] for k, v in constants.describe()]))
    return "\n".join(out) + "\n"


def _section_calibration(calibration) -> str:
    out = ["## 2. 复算基线（校准闸）\n",
           "用公式重算 EXPLAIN **已经给出**的那个计划，与数据库自己报的数字"
           "逐节点比对。对上了，才有资格拿同一套公式去算别的路径。\n",
           "这一步是整份报告唯一的可信度来源：下面每个数字都不是引用来的，"
           "是重新算出来又与已知答案对上的。\n"]

    rows = []
    for c in calibration.checks:
        computed = "—" if c.computed_total is None else "%.2f" % c.computed_total
        dev = "—" if c.deviation is None else "%.4f%%" % (c.deviation * 100.0)
        status = {"matched": "吻合", "mismatched": "**超差**",
                  "unmodeled": "未建模"}.get(c.status, c.status)
        if c.approximate:
            status += "（含估算）"
        rows.append([render.truncate(c.node_type, 40), c.relation or "—",
                     computed, "%.2f" % c.measured_total, dev, status])
    out.append(render.table(
        ["算子", "对象", "复算", "实测", "相对偏差", "判定"], rows))
    out.append("\n%s\n" % calibration.summary())
    out.append("采用的内核版本变体：min_io_split_seq=%s、btree_page_cpu_cost=%s"
               "（四种变体逐个试出来的，不是假设的）\n"
               % (calibration.variant.min_io_split_seq,
                  calibration.variant.btree_page_cpu_cost))

    unmodeled = [c for c in calibration.checks if c.status == _cal.UNMODELED]
    if unmodeled:
        out.append("\n### 没有参与校准的算子\n")
        out.append("覆盖率 %.0f%% 的意思就是「验过这么多」，剩下的**没验**。"
                   "把没验的当验过了，是这类报告最容易出的错。\n"
                   % (calibration.coverage * 100.0))
        out.append(render.table(
            ["算子", "实测代价", "原因"],
            [[render.truncate(c.node_type, 40), "%.2f" % c.measured_total,
              c.reason] for c in unmodeled]))

    # 只在真有话说的时候才起这一节 —— 空标题会让人以为「没有估算成分」，
    # 而实际上是「有估算但没说明」，两者相差很远。
    approx_notes = [(c, note) for c in calibration.checks if c.approximate
                    for note in (getattr(c.estimate, "notes", []) or [])]
    if approx_notes:
        out.append("\n### 含估算成分的节点\n")
        out.append("这些节点的某一项不是精确算出来的。它们**照样参加校准**，"
                   "标出来只是为了对不上时先查这里。\n")
        for check, note in approx_notes:
            out.append("- **%s**：%s" % (check.node_type, note))
        out.append("")

    out.append("\n### 逐项算式\n")
    out.append("每一项都代入了实际数值，可以拿计算器逐条复核。\n")
    for c in calibration.checks:
        if c.estimate is None:
            continue
        terms = getattr(c.estimate, "terms", []) or []
        if not terms:
            continue
        body = []
        width = max(len(t.label) for t in terms)
        for t in terms:
            body.append("%-*s  %-34s = %16.4f" % (width, t.label, t.formula,
                                                  t.value))
        body.append("%s" % ("─" * (width + 56)))
        body.append("%-*s  %-34s = %16.4f" % (width, "合计", "", c.computed_total))
        body.append("%-*s  %-34s = %16.4f" % (width, "实测", "", c.measured_total))
        out.append("**%s%s**\n" % (c.node_type,
                                   "（%s）" % c.relation if c.relation else ""))
        out.append(render.code_block("", "\n".join(body)))
    return "\n".join(out)


def _section_proposals(calibration, freshness: Sequence,
                       proposals: Sequence) -> str:
    out = ["## 3. 假设路径\n"]
    if not proposals:
        out.append("本次没有待评估的索引建议。\n")
        return "\n".join(out)

    ok, reason = may_emit_advice(calibration, freshness)
    if not ok:
        # **不算，而不是算了标成不可信。** 数字一旦印出来就会被读，
        # 旁边那行免责声明拦不住。
        out.append("**不计算。** 前置条件不成立：%s\n" % reason)
        out.append("> 待评估的建议：%s\n"
                   % "；".join("`%s`" % p.ddl for p in proposals))
        return "\n".join(out)

    out.append("下面的数字与第 2 节**性质不同**，不要并排看待：\n")
    out.append("- 第 2 节的复算值有实测答案可对，对上了才走到这里；\n"
               "- 这一节没有答案可对。索引还不存在，它的大小是估的；"
               "选择率也没有可反推的实测值，是按统计信息算的。\n")
    out.append("**所以这一节的每个数都是估算值。**\n")

    for i, p in enumerate(proposals, 1):
        out.append("\n### 3.%d `%s`\n" % (i, p.ddl))
        ratio = "n/a" if p.ratio is None else "%.2f×" % p.ratio
        out.append(render.table(
            ["", "代价", "来源"],
            [["基线（当前计划）", "%.2f" % p.baseline_total, "EXPLAIN 实测"],
             ["假设（建索引后）", "%.2f" % p.hypothetical_total, "**估算**"],
             ["比值", ratio, "两者不同源，比值也是估算"]]))

        terms = getattr(p.scan_estimate, "terms", []) or []
        if terms:
            width = max(len(t.label) for t in terms)
            body = ["%-*s  %-34s = %16.4f" % (width, t.label, t.formula, t.value)
                    for t in terms]
            body.append("─" * (width + 56))
            body.append("%-*s  %-34s = %16.4f"
                        % (width, "假设的索引扫描", "",
                           p.scan_estimate.total_cost))
            out.append("\n**假设的索引扫描，逐项：**\n")
            out.append(render.code_block("", "\n".join(body)))

        notes = list(getattr(p.scan_estimate, "notes", []) or [])
        for node in getattr(p.recomputed, "unmodeled", []) or []:
            notes.append(
                "上层的 %s 未建模，代价沿用实测值 —— 换了下层路径之后它本该"
                "变化，这里没算，所以总数偏保守。" % node.node_type)
        if notes:
            out.append("\n**这条建议里哪些是估的：**\n")
            for note in notes:
                out.append("- %s" % note)
            out.append("")
    return "\n".join(out)


def _section_verdict(calibration, freshness: Sequence,
                     proposals: Sequence = ()) -> str:
    ok, reason = may_emit_advice(calibration, freshness)
    out = ["## 4. 结论\n"]
    if not ok:
        out.append("**不出代价结论。**\n")
        out.append("%s\n" % reason)
        out.append("> 这里给不出数字，不是功能没做，是证据不足以支撑数字。"
                   "给一个没有背书的数字，比不给更危险 —— 它会被当成验证过的。\n")
        return "\n".join(out)
    out.append("%s\n" % reason)
    out.append("%s\n" % COST_IS_NOT_TIME)
    if proposals:
        out.append(
            "\n**基线是复算并校准过的；假设路径是估算的。** 前者说「当前这个"
            "代价是这么来的」，有实测背书；后者说「改了会变成多少」，没有。"
            "把两者当成同等可靠会高估这条建议的确定性 —— 真要背书，"
            "在直连通道上用 hypopg 实测一次。\n")
    return "\n".join(out)
