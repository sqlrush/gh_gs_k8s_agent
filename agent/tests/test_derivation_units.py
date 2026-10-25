"""推演报告与出数闸的单测。

报告本身只是渲染，真正要钉死的是 may_emit_advice：**统计不新鲜时校准可能
照样通过**（公式没错，错的是喂给公式的数），那种「通过」是最危险的一种，
因为它看起来最像验证过了。两道门的顺序不能反。
"""
import pathlib
import sys
import types

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 本仓库里 `render` / `audit` / `baseline` 这些模块名在多个 skill 下都存在,
# 而 pytest 一个进程跑完全部测试 —— 先运行的测试文件会把自己那份留在
# sys.modules 里。不先清掉,这里 `import derivation` 时它内部的 `import render`
# 就会拿到别人的 render(2026-10-18 新增 gaussdb-config 时实际撞上过:
# 本文件 62 条单独跑全过、与 test_config_audit_units.py 一起跑全挂)。
for _m in ("render", "audit", "baseline", "calibrate", "costconst", "costmodel",
           "derivation", "plantree", "model", "rules", "thresholds"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-sqltune" / "scripts"))

import calibrate  # noqa: E402
import costconst  # noqa: E402
import costmodel  # noqa: E402
import derivation  # noqa: E402
import plantree  # noqa: E402

COST = costconst.CostConstants(
    seq_page_cost=1.0, random_page_cost=1.1, cpu_tuple_cost=0.01,
    cpu_index_tuple_cost=0.005, cpu_operator_cost=0.0025, block_size=8192,
    effective_cache_size=1572864, work_mem=16 * 1024 * 1024, query_dop=2)

_PLAN = [{"Plan": {
    "Node Type": "Hash Join", "Startup Cost": 0.0, "Total Cost": 200.0,
    "Plan Rows": 10.0, "Plan Width": 8,
    "Plans": [
        {"Node Type": "Seq Scan", "Relation Name": "big", "Alias": "b",
         "Startup Cost": 0.0, "Total Cost": 100.0,
         "Plan Rows": 10.0, "Plan Width": 8},
        {"Node Type": "Sort", "Startup Cost": 0.0, "Total Cost": 50.0,
         "Plan Rows": 10.0, "Plan Width": 8},
    ]}}]
ROOT = plantree.parse(_PLAN)


def _fresh(table="big", ok=True):
    return types.SimpleNamespace(
        table=table, fresh=ok,
        reason="冻结页数 100 与实时页数 100 相差 0.0%" if ok
               else "冻结页数 100 与实时页数 900 相差 800.0%，超过阈值 10%",
        relpages=100.0, cur_pages=100.0 if ok else 900.0,
        drift=0.0 if ok else 8.0, stat_columns=6,
        reltuples=1000.0, live_tuples=1000.0,
        last_analyze="2026-08-01 03:12:44", last_autoanalyze="never")


def _calibration(passed=True, with_unmodeled=True):
    def resolve(node, children):
        if node.node_type == "Sort" and with_unmodeled:
            return None
        total = node.total_cost if passed else node.total_cost * 1.5
        return costmodel.Estimate(
            node_type=node.node_type, startup_cost=0.0, total_cost=total,
            terms=[costmodel.Term("测试项", "1 × %g" % total, total)])
    return calibrate.calibrate(ROOT, resolve)


# --- 出数闸 ------------------------------------------------------------------

def test_stale_stats_block_advice_even_when_calibration_passes():
    """**最危险的一种「通过」。** 公式没错，错的是喂给公式的数。"""
    cal = _calibration(passed=True)
    assert cal.passed is True
    ok, reason = derivation.may_emit_advice(cal, [_fresh(ok=False)])
    assert ok is False
    assert "统计信息不新鲜" in reason
    assert "800.0%" in reason      # 拒绝理由要带上可复核的数字


def test_failed_calibration_blocks_advice():
    ok, reason = derivation.may_emit_advice(_calibration(passed=False),
                                            [_fresh()])
    assert ok is False
    assert "复算未通过校准" in reason


def test_both_gates_pass_allows_advice():
    ok, reason = derivation.may_emit_advice(_calibration(), [_fresh()])
    assert ok is True
    assert "可以出代价结论" in reason


def test_no_freshness_data_is_not_a_pass():
    """拿不到新鲜度数据 = 不确定 = 按失败处理。"""
    cal = _calibration()
    report = derivation.render_report(cal, COST, [])
    assert "无法判断，按不通过处理" in report


# --- 报告内容 ----------------------------------------------------------------

def test_report_states_cost_is_not_time_when_advice_allowed():
    """这句话不能因为「显得不够自信」而删 —— 它防的是「代价降 600 倍」
    被读成「快 600 倍」。"""
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "cost 是规划器的内部单位，不是时间" in report


def test_blocked_report_says_why_and_refuses_numbers():
    report = derivation.render_report(_calibration(passed=False), COST,
                                      [_fresh()])
    assert "**不出代价结论。**" in report
    assert "给一个没有背书的数字，比不给更危险" in report


def test_report_lists_unmodeled_operators_and_coverage():
    """覆盖率就是「验过多少」，剩下的没验 —— 必须点名。"""
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "没有参与校准的算子" in report
    assert "Sort" in report
    assert "67%" in report or "66%" in report


def test_report_shows_per_term_arithmetic():
    """每一项要代入实际数值，读的人能拿计算器复核。"""
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "逐项算式" in report
    assert "测试项" in report
    assert "实测" in report


def test_report_shows_which_variant_was_used():
    """「用了哪一版公式」是测出来的，报告里要写清楚是哪一版。"""
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "min_io_split_seq" in report
    assert "不是假设的" in report


def test_report_lists_actual_cost_constants():
    """random_page_cost 在 og5 上是 1.1 不是 4.0 —— 报告要让人看得见。"""
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "random_page_cost" in report and "1.1" in report
    assert "不使用任何默认值" in report


def test_freshness_table_shows_both_page_counts():
    report = derivation.render_report(_calibration(), COST, [_fresh(ok=False)])
    assert "冻结页数" in report and "实时页数" in report
    assert "**不通过**" in report


def test_freshness_section_says_why_last_analyze_is_only_advisory():
    """last_analyze 可被 pg_stat_reset 清掉 —— 报告要说清它为什么不是判据，
    否则读的人看到「last_analyze: 无记录」却判定通过，会以为报告出错了。"""
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "pg_stat_reset" in report
    assert "仅供参考" in report or "仅供\n参考" in report


def test_report_includes_sql_when_given():
    report = derivation.render_report(_calibration(), COST, [_fresh()],
                                      sql="SELECT 1")
    assert "SELECT 1" in report


# --- 第 3 节：假设路径 -------------------------------------------------------

import whatif  # noqa: E402


def _proposal(baseline=200.0, hypothetical=20.0, unmodeled=()):
    scan = costmodel.Estimate(
        node_type="Index Scan（假设）", startup_cost=0.4, total_cost=hypothetical,
        terms=[costmodel.Term("索引读页", "1.1 × 3", 3.3)],
        approximate=True,
        notes=["索引尚不存在，页数是按列宽估算的 —— 估小了会低估索引扫描 IO。",
               "选择率来自统计信息估算，不是实测。"])
    rec = whatif.Recomputed(root_total=hypothetical, estimates=[],
                            unmodeled=list(unmodeled))
    return whatif.Proposal(ddl="CREATE INDEX i ON t(c)", table="t", column="c",
                           baseline_total=baseline,
                           hypothetical_total=hypothetical,
                           scan_estimate=scan, recomputed=rec)


def test_no_proposals_says_so():
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "本次没有待评估的索引建议" in report


def test_proposals_are_not_computed_when_gates_fail():
    """**不算，而不是算了标成不可信。**

    数字一旦印出来就会被读，旁边那行免责声明拦不住。所以门没过时
    连数都不出，只把待评估的 DDL 列出来。
    """
    report = derivation.render_report(_calibration(), COST, [_fresh(ok=False)],
                                      proposals=[_proposal()])
    assert "**不计算。**" in report
    assert "CREATE INDEX i ON t(c)" in report
    assert "20.00" not in report          # 假设代价不能出现


def test_proposal_marks_baseline_and_hypothetical_as_different_kinds():
    """一个是 EXPLAIN 实测，一个是估算 —— 不能并排同等呈现。"""
    report = derivation.render_report(_calibration(), COST, [_fresh()],
                                      proposals=[_proposal()])
    assert "EXPLAIN 实测" in report
    assert "**估算**" in report
    assert "两者不同源，比值也是估算" in report
    assert "10.00×" in report             # 200 / 20


def test_proposal_lists_what_was_estimated():
    report = derivation.render_report(_calibration(), COST, [_fresh()],
                                      proposals=[_proposal()])
    assert "这条建议里哪些是估的" in report
    assert "索引尚不存在" in report
    assert "选择率" in report


def test_unmodeled_ancestor_is_disclosed_as_conservative():
    """上层没重算的话总数偏保守 —— 不说破，读的人会以为算全了。"""
    node = types.SimpleNamespace(node_type="Merge Join")
    report = derivation.render_report(_calibration(), COST, [_fresh()],
                                      proposals=[_proposal(unmodeled=[node])])
    assert "Merge Join 未建模" in report
    assert "偏保守" in report


def test_verdict_separates_calibrated_from_estimated():
    report = derivation.render_report(_calibration(), COST, [_fresh()],
                                      proposals=[_proposal()])
    assert "基线是复算并校准过的；假设路径是估算的" in report
    assert "hypopg 实测一次" in report


def test_verdict_section_is_numbered_four():
    report = derivation.render_report(_calibration(), COST, [_fresh()])
    assert "## 4. 结论" in report
    assert "## 3. 假设路径" in report
