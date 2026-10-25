"""EXPLAIN ANALYZE 到底有没有真跑:看计划文本,不看请求标志。

现场三条 *_analyze 注册脚本按客户只读要求把 ANALYZE 固定成 false(17dde8c),用户要了 --analyze
拿到的仍是估算计划。skill 不能凭「我请求了 analyze」就说这是实测——要看计划里有没有 actual 行。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import explain_actual as ea  # noqa: E402

ESTIMATE = "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)"
ACTUAL = "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4) (actual time=0.010..0.012 rows=1 loops=1)"


def test_has_actual_looks_at_the_plan_text():
    assert ea.has_actual(ACTUAL)
    assert not ea.has_actual(ESTIMATE)
    assert not ea.has_actual("")


def test_analyzed_for_real_needs_both_request_and_actual_rows():
    assert ea.analyzed_for_real(ACTUAL, True)
    assert not ea.analyzed_for_real(ESTIMATE, True)     # 要了却没跑:现场脚本关了 ANALYZE
    assert not ea.analyzed_for_real(ACTUAL, False)      # 没要:不算 analyze(不该发生,但别撒谎)


def test_note_only_when_analyze_was_requested_but_not_run():
    note = ea.analyze_note(ESTIMATE, True)
    assert "ANALYZE" in note and "估算" in note and "只读" in note   # 说清原因:客户只读要求
    assert "实测" in note                                            # 提醒引用口径
    assert ea.analyze_note(ACTUAL, True) == ""
    assert ea.analyze_note(ESTIMATE, False) == ""
