"""表膨胀**估算**的判定（纯函数，不连库）。

**这组测试的核心是那条陈旧校验。** `relpages` / `reltuples` 来自最后一次
ANALYZE/VACUUM，可能严重陈旧 —— og5 实测 `snapshot.statement_history` 的
`relpages=428232`，而真实只有 14 页（表被清空过但没重新 analyze）。
拿这个 reltuples 去估算，会算出天文数字的"膨胀率"，它会排在报告第一位、
看起来是最该处理的一张表，而真相是统计陈旧、表其实是空的。

所以 `estimate()` 必须先比 relpages 与真实页数：差得过多就**拒绝估算**，
返回原因而不是数字。一旦哪天有人把这道校验去掉（或者改成"用真实页数反推
reltuples"这种看似聪明的写法），报告就会开始输出很严重的假数字。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-vacuum" / "scripts"

for _m in ("bloat", "freeze", "rules", "thresholds", "render", "vacuum"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import bloat as BL  # noqa: E402


def _row(schema="fl", table="t", relpages=10000, real_pages=10000,
         reltuples=1000000, real_bytes=None, width=50):
    return {"schema": schema, "table": table, "relpages": relpages,
            "real_pages": real_pages, "reltuples": reltuples,
            "real_bytes": real_bytes if real_bytes is not None else real_pages * 8192,
            "avg_row_width": width}


# ---------------------------------------------------------------- 陈旧校验

def test_stale_stats_refuse_to_estimate_instead_of_reporting_a_fake_number():
    """**og5 实测的真实一行**：统计里 428232 页，实际 14 页。

    不拦的话这张表会报出一个巨大的膨胀率、排在报告第一位，而它其实是空的。
    """
    e = BL.estimate(_row(schema="snapshot", table="statement_history",
                         relpages=428232, real_pages=14, reltuples=3594,
                         real_bytes=114688, width=100))
    assert e.pct is None
    assert "统计已陈旧" in e.reason and "428232" in e.reason and "14" in e.reason


def test_the_staleness_check_works_in_both_directions():
    """统计比实际**小**得多也一样不可信（表长大了但没 analyze）。"""
    assert BL.estimate(_row(relpages=100, real_pages=10000)).pct is None


def test_a_mild_difference_still_estimates():
    """正常的增删改会让统计略微偏离 —— 这种程度不该拒绝估算，否则这项等于没有。"""
    e = BL.estimate(_row(relpages=10000, real_pages=11000))
    assert e.pct is not None


def test_the_boundary_of_the_staleness_check():
    assert BL.estimate(_row(relpages=10000,
                            real_pages=int(10000 * BL.STALE_RATIO) - 1)).pct is not None
    assert BL.estimate(_row(relpages=10000,
                            real_pages=int(10000 * BL.STALE_RATIO) + 100)).pct is None


# ---------------------------------------------------------------- 估不了的其它情形

def test_a_table_never_analyzed_has_no_row_width_so_it_is_skipped():
    e = BL.estimate(_row(width=""))
    assert e.pct is None and "从没 ANALYZE 过" in e.reason


@pytest.mark.parametrize("kw,word", [
    ({"relpages": 0}, "页数为 0"),
    ({"real_pages": 0}, "真实页数"),
    ({"width": 0}, "每行多宽"),
])
def test_missing_inputs_are_explained_not_guessed(kw, word):
    e = BL.estimate(_row(**kw))
    assert e.pct is None and word in e.reason


def test_skipped_lists_every_table_that_could_not_be_estimated():
    """**估不了的表也要列出来。** 不列等于让人以为所有表都估过了 ——
    而统计陈旧的那几张恰恰最可能真的在膨胀。"""
    rows = [_row(table="ok"), _row(table="stale", relpages=400000, real_pages=10),
            _row(table="noanalyze", width="")]
    names = [e.table for e in BL.skipped(rows)]
    assert names == ["stale", "noanalyze"]


# ---------------------------------------------------------------- 估算本身

def test_a_healthy_table_estimates_near_zero():
    """og5 实测的 fl.order_items：3,000,000 行 × 29 字节，22223 页 —— 约 6%。"""
    e = BL.estimate(_row(table="order_items", relpages=22223, real_pages=22223,
                         reltuples=3000000, width=29))
    assert e.pct is not None and -20 < e.pct < 30


def test_a_table_taking_twice_its_expected_space_is_a_warning():
    # 1,000,000 行 × 50 字节 ≈ 9550 页应占;给它 4 万页(327MB,过 MIN_BYTES)→ 膨胀 >300%
    fs = BL.judge([_row(relpages=40000, real_pages=40000, reltuples=1000000,
                        width=50, real_bytes=40000 * 8192)])
    assert [f.severity.name for f in fs] == ["WARN"]


def test_the_notice_and_warn_bands_are_distinguishable():
    """50% 关注、100% 告警。两档都要能到达,否则其中一档是死代码。"""
    # 应占 9550 页;×1.6 ≈ 15280 页 → 60%(NOTICE)。但 15280 页只有 119MB,
    # 过不了 MIN_BYTES —— 所以把行数放大一个量级,让表够大而比例不变。
    notice = BL.judge([_row(relpages=96000, real_pages=96000, reltuples=6300000,
                            width=50, real_bytes=96000 * 8192)])
    assert [f.severity.name for f in notice] == ["NOTICE"], \
        "NOTICE 档到不了的话它就是死代码：%s" % [(f.severity.name, f.value) for f in notice]


def test_a_small_table_is_not_reported_however_bloated():
    """几十 MB 的表膨胀一倍也只是几十 MB，而重建要锁表 —— 不值当。"""
    fs = BL.judge([_row(relpages=3000, real_pages=3000, reltuples=10000,
                        width=50, real_bytes=3000 * 8192)])
    assert fs == []


def test_findings_are_capped_and_sorted_worst_first():
    rows = [_row(table="t%d" % i, relpages=40000 + i * 1000,
                 real_pages=40000 + i * 1000, reltuples=500000, width=50,
                 real_bytes=(40000 + i * 1000) * 8192) for i in range(10)]
    fs = BL.judge(rows)
    assert len(fs) == BL.MAX_FINDINGS == 5
    assert "t9" in fs[0].metric          # 最膨胀的排最前


# ---------------------------------------------------------------- 措辞

def test_the_evidence_says_it_is_an_estimate_not_a_measurement():
    """本内核没装 pgstattuple，这是推算 —— 不写明就会被当成测量值。"""
    fs = BL.judge([_row(relpages=40000, real_pages=40000, reltuples=500000,
                        width=50, real_bytes=40000 * 8192)])
    assert "估算不是测量" in fs[0].evidence and "pgstattuple" in fs[0].evidence


def test_the_evidence_distinguishes_bloat_from_dead_tuples():
    """死元组高 → VACUUM 能回收；死元组低而膨胀高 → VACUUM 不还盘，要 VACUUM FULL。
    两件事混在一起，处置就会错（跑一次没用的 VACUUM）。"""
    fs = BL.judge([_row(relpages=40000, real_pages=40000, reltuples=500000,
                        width=50, real_bytes=40000 * 8192)])
    ev = fs[0].evidence
    assert "VACUUM 不还盘" in ev and "锁表" in ev


def test_no_reclaimable_space_promise():
    """和 vacuum 主报告同一条纪律：绝不承诺「可回收 X GB」——
    真能回收多少只有跑一次才知道，而猜测值摆在真实测量值旁边会被当成承诺。"""
    fs = BL.judge([_row(relpages=40000, real_pages=40000, reltuples=500000,
                        width=50, real_bytes=40000 * 8192)])
    for bad in ("可回收", "预计释放", "将释放"):
        assert bad not in fs[0].evidence
