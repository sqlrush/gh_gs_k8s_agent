"""事务号冻结水位的判定（纯函数，不连库）。

**这组测试守着一个「从 PostgreSQL 搬错结论」的坑。** PG 上 frozenxid 追到 2^31
会让库强制停写，抢救要停机。写这个检查时我差点照搬这个结论，实测 og5 之后才发现
不成立：`autovacuum_freeze_max_age` 默认 4000000000、`vacuum_freeze_min_age`
默认 2000000000（两个都超过 2^31），而且没有 `xid_stop_limit` —— 这个内核用的是
64 位事务号。

所以下面有一条测试专门钉住「报告里不许出现『停写』这类说法」。把一个 🟡 报成 🔴，
虚报一次之后真该紧张的时候就没人信了 —— 这和这套工具一直在修的那类错是同一种。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-vacuum" / "scripts"

for _m in ("freeze", "rules", "thresholds", "render", "vacuum"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import freeze as F  # noqa: E402


def _row(name="postgres", age="2000000000", cap="4000000000"):
    return {"datname": name, "freeze_age": age, "max_age": cap,
            "freeze_table_age": cap}


# ---------------------------------------------------------------- 占比

def test_pct_is_against_this_databases_own_max_age():
    """分母用**这个库自己配的** autovacuum_freeze_max_age，不是写死的常数。

    客户把它调小（常见做法，为了让冻结更早更分散）时，只有用它做分母，
    占比才真正回答「离强制冻结还有多远」。
    """
    assert F.freeze_pct(_row(age="2000000000", cap="4000000000")) == pytest.approx(50.0)
    assert F.freeze_pct(_row(age="2000000000", cap="2500000000")) == pytest.approx(80.0)


@pytest.mark.parametrize("row", [
    {"datname": "x", "freeze_age": "", "max_age": "4000000000"},
    {"datname": "x", "freeze_age": "1", "max_age": ""},
    {"datname": "x", "freeze_age": "1", "max_age": "0"},
    {"datname": "x", "freeze_age": "1", "max_age": "-5"},
    {"datname": "x"},
])
def test_an_uncomputable_pct_is_minus_one_not_zero(row):
    """返回 0 会让「算不出来」和「刚冻结过、水位很低」这两个相反的事实混在一起 ——
    然后报告会说「冻结水位 0%，很健康」，而真相是分母没取到。"""
    assert F.freeze_pct(row) == -1.0


# ---------------------------------------------------------------- 判定

def test_a_low_watermark_reports_nothing():
    """og5 实测是 3.71% —— 这种水位不该出现在报告里。"""
    assert F.judge([_row(age="148366977")]) == []


def test_sixty_percent_is_a_notice_and_eighty_five_is_a_warning():
    assert F.judge([_row(age="2400000000")])[0].severity.name == "NOTICE"   # 60%
    assert F.judge([_row(age="3400000000")])[0].severity.name == "WARN"     # 85%


def test_nothing_is_ever_critical_on_this_kernel():
    """**这是本组最重要的一条。** 这个内核是 64 位事务号，冻结水位高不会导致停写。
    报 🔴 是虚报，而虚报会让这条发现以后被当成噪音。"""
    for age in ("3900000000", "4000000000", "8000000000"):
        for f in F.judge([_row(age=age)]):
            assert f.severity.name != "CRITICAL"


def test_a_write_stop_is_only_ever_mentioned_to_rule_it_out():
    """证据里提到「停写 / 2^31」时**必须是在否定它**。

    读报告的 DBA 多半是 PG 背景，看到「冻结水位 85%」就会去做一次没必要的停机抢救。
    所以这里不是禁止提这个词 —— 恰恰相反，要主动点明「本内核不会这样」；
    禁止的是把它当成一个会发生的后果来陈述。
    """
    ev = F.judge([_row(age="3900000000")])[0].evidence
    for word in ("停写", "2^31"):
        if word in ev:
            head = ev[:ev.index(word)]
            assert "不会" in head, "提到「%s」却没有否定它：%s" % (word, ev)
    # 这些说法一次都不该出现 —— 它们只会把人往停机抢救的方向推
    for never in ("拒绝写入", "停机抢救", "单用户模式"):
        assert never not in ev


def test_the_evidence_says_what_actually_happens():
    """真实后果是全表冻结扫描（不择时的 IO 风暴），这句必须在。"""
    ev = F.judge([_row(age="3900000000")])[0].evidence
    assert "全表冻结扫描" in ev and "64 位" in ev


def test_databases_are_reported_worst_first():
    rows = [_row("low", age="2400000000"), _row("high", age="3900000000")]
    out = F.judge(rows)
    assert out[0].metric.startswith("库 high")


def test_at_most_three_databases_are_reported():
    """一个实例上几十个库同时偏高时，刷满报告反而看不见重点。"""
    rows = [_row("db%d" % i, age="3900000000") for i in range(10)]
    assert len(F.judge(rows)) == F.MAX_FINDINGS == 3


def test_a_row_that_cannot_be_scored_is_skipped_not_reported():
    rows = [{"datname": "bad", "freeze_age": "", "max_age": ""},
            _row("good", age="3900000000")]
    out = F.judge(rows)
    assert len(out) == 1 and "good" in out[0].metric


@pytest.mark.parametrize("rows", [None, [], [{}]])
def test_no_usable_rows_means_no_findings(rows):
    assert F.judge(rows) == []


def test_worst_pct_ignores_uncomputable_rows():
    rows = [{"datname": "bad", "freeze_age": "", "max_age": ""},
            _row(age="2000000000")]
    assert F.worst_pct(rows) == pytest.approx(50.0)


def test_worst_pct_is_minus_one_when_nothing_can_be_computed():
    """返回 0 会让抬头写成「最高 0%」—— 那是个没有依据的数字。"""
    assert F.worst_pct([{"datname": "bad"}]) == -1.0
