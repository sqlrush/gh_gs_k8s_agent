"""autovacuum 触发线与四条手工清理规则 —— 纯函数，不连库。

rules/thresholds 这两个模块名在别的 skill 下也存在（sqlreview 有 rules.py，
health/memanalyze/wdr 都有 thresholds.py，内容各不相同）。整个测试套件一起
跑时，先被收集到的那份会把 'rules'/'thresholds' 钉在 sys.modules 里，这个
文件的 `from rules import ...` 就会静默拿到别的 skill 的同名模块 —— 抄
tests/test_health_units.py 的处理：导入前先把这两个名字从缓存里踢出去。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-vacuum" / "scripts"

for _m in ("rules", "thresholds"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

from common.finding import Severity  # noqa: E402
from rules import default_thresholds, evaluate, judge_tables, trigger_line  # noqa: E402

_SETTINGS = {"autovacuum_vacuum_threshold": "50",
             "autovacuum_vacuum_scale_factor": "0.2",
             "autovacuum_naptime": "30"}
MB = 1024 * 1024


def _tbl(**kw):
    base = dict(schema="public", table="t", n_live_tup=1000, n_dead_tup=100,
                reltuples=1000, table_bytes=200 * MB,
                last_autovacuum_age_s=None, last_vacuum_age_s=None,
                vacuum_count=0, autovacuum_count=0,
                autovac_enabled=True, reloptions="")
    base.update(kw)
    return base


def test_trigger_line_uses_the_real_gucs():
    """50 + 0.2 × 1000 = 250。参数从 pg_settings 实读，不写死。"""
    assert trigger_line(1000, _SETTINGS, "") == 250.0


def test_table_level_reloptions_override_the_global_scale_factor():
    assert trigger_line(1000, _SETTINGS,
                        "autovacuum_vacuum_scale_factor=0.05") == 100.0


def test_table_level_threshold_also_overrides():
    assert trigger_line(1000, _SETTINGS,
                        "autovacuum_vacuum_threshold=500") == 700.0


def test_r1_hits_when_over_the_line_and_never_autovacuumed():
    hit = evaluate(_tbl(n_dead_tup=300, last_autovacuum_age_s=None),
                   _SETTINGS, [], default_thresholds())
    assert "R1" in hit


def test_r1_hits_when_over_the_line_and_overdue():
    hit = evaluate(_tbl(n_dead_tup=300, last_autovacuum_age_s=7200),
                   _SETTINGS, [], default_thresholds())
    assert "R1" in hit


def test_r1_does_not_hit_when_autovacuum_just_ran():
    """刚跑过就不是「没跟上」—— 死元组还在只是因为还没来得及。"""
    hit = evaluate(_tbl(n_dead_tup=300, last_autovacuum_age_s=60),
                   _SETTINGS, [], default_thresholds())
    assert "R1" not in hit


def test_r1_treats_zero_age_as_just_ran_not_as_unknown():
    """0 秒和 NULL 是相反的事实：0 秒是「刚跑完」，NULL 是「从没跑过」。

    `last_autovacuum_age_s or default` 这类写法会把 0 当成假值，本该判「刚跑过、
    不算没追上」的表会被 `or` 吞掉真正的 0 而走上另一条分支。这里死元组早过了
    触发线，但 0 秒前刚服务过，必须判「没有没追上」。
    """
    hit = evaluate(_tbl(n_dead_tup=300, last_autovacuum_age_s=0),
                   _SETTINGS, [], default_thresholds())
    assert "R1" not in hit


def test_r1_does_not_hit_below_the_trigger_line():
    hit = evaluate(_tbl(n_dead_tup=100, last_autovacuum_age_s=None),
                   _SETTINGS, [], default_thresholds())
    assert "R1" not in hit


def test_r2_hits_when_autovacuum_disabled_on_the_table():
    hit = evaluate(_tbl(autovac_enabled=False,
                        reloptions="autovacuum_enabled=false"),
                   _SETTINGS, [], default_thresholds())
    assert "R2" in hit


def test_r3_hits_on_high_ratio_and_big_table():
    hit = evaluate(_tbl(n_live_tup=1000, n_dead_tup=1000, table_bytes=200 * MB),
                   _SETTINGS, [], default_thresholds())
    assert "R3" in hit


def test_r3_does_not_hit_on_a_tiny_table():
    """比例再高，1 MB 的表也不值得让人半夜起来处理。"""
    hit = evaluate(_tbl(n_live_tup=1000, n_dead_tup=1000, table_bytes=1 * MB),
                   _SETTINGS, [], default_thresholds())
    assert "R3" not in hit


def test_r4_hits_when_an_old_transaction_blocks_reclaim():
    xmin = [{"source": "long_xact", "identifier": "2259",
             "xmin_age_s": "3600", "detail": "idle in transaction"}]
    hit = evaluate(_tbl(n_dead_tup=300), _SETTINGS, xmin, default_thresholds())
    assert "R4" in hit


def test_r4_evidence_says_vacuum_would_not_help():
    """**这是 R4 存在的全部理由。** 不说这句，用户会去跑一条没用的 VACUUM。"""
    xmin = [{"source": "long_xact", "identifier": "2259",
             "xmin_age_s": "3600", "detail": "idle in transaction"}]
    fs = judge_tables([_tbl(n_dead_tup=300)], _SETTINGS, xmin,
                      default_thresholds())
    r4 = [f for f in fs if "R4" in f.evidence or f.code.endswith("XMIN_BLOCKED")]
    assert r4, "R4 没产出 finding"
    assert "回收不掉" in r4[0].evidence or "也没用" in r4[0].evidence


def test_r4_fires_on_a_replication_slot_with_no_usable_age():
    """复制槽的 xmin_age_s 恒为空 —— 不是取数失败，是 pg_replication_slots 在这
    个内核上压根没有时间戳列（vacuum.oldest_xmin.yaml 里逐列核对过
    information_schema）。R4 的判定绝不能依赖「拿到一个能比较大小的数字」，
    只能依赖「这个阻塞源存在」。

    这条测试只放一条 replication_slot、`xmin_age_s` 是空串，且不掺任何带
    真实 age 的 long_xact/prepared_xact —— 哪天有人把判定改写成类似
    `if age and as_float(age) > 0` 这种依赖数值比较的写法，复制槽这一整类
    阻塞源会从判定里静默消失，这条测试会立刻变红。
    """
    xmin = [{"source": "replication_slot", "identifier": "rep_slot_1",
             "xmin_age_s": "", "detail": "xmin=12345 catalog_xmin=12300 active=t"}]
    hit = evaluate(_tbl(n_dead_tup=300), _SETTINGS, xmin, default_thresholds())
    assert "R4" in hit


def test_r4_evidence_says_age_is_unknown_not_recent_for_a_replication_slot():
    """看到 `xmin_age_s` 是空的，不能读成「这个复制槽刚连上、问题不大」——
    它照样在挡回收，只是这张视图没有时间戳列，算不出挡了多久。evidence 必须
    明说「未知」，不能干脆不提年龄，更不能编造一个数字冒充「刚连上」。
    """
    xmin = [{"source": "replication_slot", "identifier": "rep_slot_1",
             "xmin_age_s": "", "detail": "xmin=12345 catalog_xmin=12300 active=t"}]
    fs = judge_tables([_tbl(n_dead_tup=300)], _SETTINGS, xmin,
                      default_thresholds())
    r4 = [f for f in fs if f.code.endswith("XMIN_BLOCKED")]
    assert r4, "复制槽阻塞没有产出 R4 finding"
    assert "未知" in r4[0].evidence
    assert "复制槽" in r4[0].evidence
    assert "rep_slot_1" in r4[0].evidence


def test_r4_does_not_suppress_r1_and_r3_it_only_warns_them():
    """R4 命中时其余规则照常命中 —— 不是「只报 R4」，是给 R1/R3 的 evidence 多
    缀一句提示。压制掉 R1/R3 会把真实的膨胀问题从报告里藏起来；这条测试直接
    核对三个 code 同时出现，而不是只信一句注释。
    """
    xmin = [{"source": "long_xact", "identifier": "2259",
             "xmin_age_s": "3600", "detail": "idle in transaction"}]
    tbl = _tbl(schema="gsbench_e2e_20260801_100g", table="plan_data",
               n_live_tup=20178297, n_dead_tup=20087028,
               reltuples=20178297, table_bytes=8 * 1024 * MB,
               last_autovacuum_age_s=None, autovacuum_count=0)
    fs = judge_tables([tbl], _SETTINGS, xmin, default_thresholds())
    codes = {f.code for f in fs}
    assert {"VACUUM_OVERDUE", "VACUUM_DEAD_RATIO", "VACUUM_XMIN_BLOCKED"} <= codes
    r1 = next(f for f in fs if f.code == "VACUUM_OVERDUE")
    r3 = next(f for f in fs if f.code == "VACUUM_DEAD_RATIO")
    assert "回收不掉" in r1.evidence or "也没用" in r1.evidence
    assert "回收不掉" in r3.evidence or "也没用" in r3.evidence


def test_the_measured_case_hits_r1_and_r3():
    """og5 上的真实案例：plan_data 活 20178297 / 死 20087028，从没 autovacuum 过。"""
    hit = evaluate(_tbl(schema="gsbench_e2e_20260801_100g", table="plan_data",
                        n_live_tup=20178297, n_dead_tup=20087028,
                        reltuples=20178297, table_bytes=8 * 1024 * MB,
                        last_autovacuum_age_s=None, autovacuum_count=0),
                   _SETTINGS, [], default_thresholds())
    assert "R1" in hit and "R3" in hit


def test_crit_ratio_is_more_severe_than_warn_ratio():
    fs_warn = judge_tables([_tbl(n_live_tup=1000, n_dead_tup=300,
                                 table_bytes=200 * MB)],
                           _SETTINGS, [], default_thresholds())
    fs_crit = judge_tables([_tbl(n_live_tup=1000, n_dead_tup=1000,
                                 table_bytes=200 * MB)],
                           _SETTINGS, [], default_thresholds())
    assert max(f.severity for f in fs_crit) > max(f.severity for f in fs_warn)


def test_clean_table_produces_no_findings():
    assert judge_tables([_tbl(n_dead_tup=10, last_autovacuum_age_s=30)],
                        _SETTINGS, [], default_thresholds()) == []


def test_each_table_finding_names_its_table():
    """两张表都关了 autovacuum 时,两条发现的文字一模一样,看起来像重复 —— 证据里要写是哪张表。

    2026-09-23 大盘真数据测试:健康检查大盘上「autovacuum_enabled = false」连着出现两条,
    无从分辨,用户只会当成 bug。R1/R2/R3 都是按表的发现,都要带表名。
    """
    a = _tbl(schema="biz", table="orders", autovac_enabled=False, reloptions="autovacuum_enabled=false")
    b = _tbl(schema="biz", table="events", autovac_enabled=False, reloptions="autovacuum_enabled=false")
    fs = judge_tables([a, b], _SETTINGS, [], default_thresholds())
    r2 = [f for f in fs if f.metric == "autovacuum_enabled"]
    assert len(r2) == 2
    assert "biz.orders" in r2[0].evidence and "biz.events" in r2[1].evidence
    big = _tbl(schema="biz", table="fact", n_live_tup=1000, n_dead_tup=1000, table_bytes=200 * MB)
    for f in judge_tables([big], _SETTINGS, [], default_thresholds()):
        if f.code != "VACUUM_XMIN_BLOCKED":
            assert f.evidence.startswith("biz.fact"), f.evidence
