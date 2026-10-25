"""DB-free unit tests for the wdr skill (model JSON round-trip, recheck gate,
report rendering, thresholds).

wdr shares module names (model/thresholds/util/collectors/report) with the
health skill but with different contents, so we purge any cached copies and put
wdr's scripts dir first on sys.path before importing by real module name.
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-wdr" / "scripts"

for _m in ("model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import finalreport  # noqa: E402
import interp as interp_mod  # noqa: E402
import model  # noqa: E402
import recheck as recheck_mod  # noqa: E402
import report  # noqa: E402
import thresholds  # noqa: E402
from interp import Interp, InterpFinding, InterpOverall, Suggestion, Validation  # noqa: E402
from model import (  # noqa: E402
    DimResult, Evidence, Finding, NativeInfo, Severity, Window,
)


def _ev_with(findings, overall):
    return Evidence(conn="og", window=Window(begin_id=1, end_id=2, scope="node"),
                    dims=[], findings=findings, overall=overall)


def test_evidence_json_round_trip():
    f = Finding(model.DIM_TOPSQL, "WDR_TOPSQL_DBTIME", Severity.WARN,
               "单条 SQL 占 DB time", "60.00%", ">50%", "evidence", sql_id="123")
    dim = DimResult(dimension=model.DIM_TOPSQL, available=True, headline="h",
                    headers=["a", "b"], rows=[["1", "2"]], findings=[f])
    ev = Evidence(conn="og", target="og5",
                  window=Window(begin_id=687, end_id=688, begin_ts="t1", end_ts="t2",
                                duration_min=161, scope="node", node="og5", wdr_enabled=True),
                  dims=[dim], findings=[f], overall=Severity.WARN,
                  native=NativeInfo(generated=True, saved_path="/x", bytes=42))
    back = Evidence.from_dict(ev.to_dict())
    assert back.to_dict() == ev.to_dict()
    assert back.overall == Severity.WARN
    assert back.findings[0].sql_id == "123"
    assert back.window.duration_min == 161
    assert back.dims[0].rows == [["1", "2"]]


def test_finding_always_emits_all_nine_keys():
    """曾经的行为：`sql_id` 为空就整个键都不出现在 `to_dict()` 里。

    Finding 归一到 `common.finding` 之后这条不再成立 —— `to_dict()` 现在是
    **跨进程契约**：`gaussdb-health` 会把每个 skill 当子进程拉起来，解析它
    `--format json` 的输出。键集合随内容变化，意味着每个消费者都得同时兼容
    「有这个键」和「没有这个键」两种形状；不兼容时的失败模式是把缺的字段
    读成「没有值」——静默，不是报错。键集合稳定正是 Task 1 的全部意义所在，
    把 `to_dict()` 改回按内容有条件地出键，等于把这件事撤销。

    代价是真实的，但小且可接受：wdr 的 json 输出从此会多出
    `sql_id: ""`、`skill: ""`（原先未设置时这两个键根本不出现）。
    用 `d["sql_id"]` 读的代码不受影响；只有 `"sql_id" in d` 这种存在性判断
    的语义变了。
    """
    f = Finding("d", "C", Severity.NOTICE, "m", "v", "t", "e")
    d = f.to_dict()
    assert set(d) == {"dimension", "code", "severity", "metric", "value",
                      "threshold", "evidence", "sql_id", "skill"}
    assert d["sql_id"] == "", "未设置时留空串，而不是省略这个键"
    assert d["skill"] == "", "未设置时留空串，而不是没有这个键"

    f2 = Finding("d", "C", Severity.NOTICE, "m", "v", "t", "e", sql_id="9")
    assert f2.to_dict()["sql_id"] == "9"


def test_parse_severity():
    assert interp_mod.parse_severity("CRITICAL") == Severity.CRITICAL
    assert interp_mod.parse_severity("warn") == Severity.WARN
    assert interp_mod.parse_severity(" notice ") == Severity.NOTICE
    assert interp_mod.parse_severity("bogus") == Severity.OK
    assert interp_mod.parse_severity("") == Severity.OK


def test_suggestion_verified_and_proven():
    sv = recheck_mod.suggestion_verified
    sp = recheck_mod.suggestion_proven
    advisory = Suggestion(text="x", validation=None)
    assert sv(advisory) and not sp(advisory)
    hypopg_ok = Suggestion(text="x", validation=Validation("hypopg", "verified", "e"))
    assert sv(hypopg_ok) and sp(hypopg_ok)
    hypopg_fail = Suggestion(text="x", validation=Validation("hypopg", "failed", ""))
    assert not sv(hypopg_fail) and not sp(hypopg_fail)
    none_method = Suggestion(text="x", validation=Validation("none", "n/a", ""))
    assert sv(none_method) and not sp(none_method)


def test_recheck_clean_badge():
    f = Finding(model.DIM_DBSTAT, "WDR_DEADLOCK", Severity.WARN, "死锁数", "6", "≥5", "e")
    ev = _ev_with([f], Severity.WARN)
    in_ = Interp(overall=InterpOverall("WARN", "死锁多发"),
                 findings=[InterpFinding(code="WDR_DEADLOCK", root_cause="加锁顺序不一致",
                                         suggestions=[Suggestion("统一加锁顺序", "低", True,
                                                                 Validation("none", "n/a", ""))])])
    r = recheck_mod.recheck(ev, in_)
    assert r.overall == Severity.WARN
    assert len(r.anchored) == 1 and not r.unanchored and not r.missing
    assert not r.sev_mismatch
    assert r.badge.startswith("✓ 已锚定")
    assert "1 条建议性" in r.badge  # advisory, not proven


def test_recheck_missing_unanchored_mismatch():
    warn = Finding(model.DIM_DBSTAT, "WDR_DEADLOCK", Severity.WARN, "死锁数", "6", "≥5", "e")
    ev = _ev_with([warn], Severity.WARN)
    # interp covers a non-existent code, declares OK (mismatch), and omits the WARN
    in_ = Interp(overall=InterpOverall("OK", "看起来还行"),
                 findings=[InterpFinding(code="WDR_BOGUS", root_cause="x")])
    r = recheck_mod.recheck(ev, in_)
    assert [f.code for f in r.missing] == ["WDR_DEADLOCK"]
    assert [inf.code for inf in r.unanchored] == ["WDR_BOGUS"]
    assert r.sev_mismatch is True
    assert r.badge.startswith("⚠ 校验有偏差")
    assert "漏报 1 条" in r.badge and "未锚定 1 条" in r.badge


def test_recheck_overall_never_inflated_by_interp():
    notice = Finding(model.DIM_DBSTAT, "WDR_DEADLOCK", Severity.NOTICE, "死锁数", "2", "≥1", "e")
    ev = _ev_with([notice], Severity.NOTICE)
    in_ = Interp(overall=InterpOverall("CRITICAL", "夸大"),
                 findings=[InterpFinding(code="WDR_DEADLOCK", root_cause="x")])
    r = recheck_mod.recheck(ev, in_)
    assert r.overall == Severity.NOTICE  # evidence is authoritative
    assert r.sev_mismatch is True


def test_render_report_md_sections():
    f = Finding(model.DIM_WAITS, "WDR_WAIT_CLASS_SKEW", Severity.WARN, "等待类倾斜",
               "IO 98%", ">60%", "snap_global_wait_events")
    dim = DimResult(dimension=model.DIM_WAITS, available=True, headline="Top 等待类 IO 占 98%",
                    headers=["等待类", "占比%"], rows=[["IO", "98"]], findings=[f])
    ev = Evidence(conn="og", window=Window(begin_id=687, end_id=688, scope="node", node="og5"),
                  dims=[dim], findings=[f], overall=Severity.WARN)
    in_ = Interp(overall=InterpOverall("WARN", "IO 主导"),
                 findings=[InterpFinding(code="WDR_WAIT_CLASS_SKEW", root_cause="物理读瓶颈",
                                         suggestions=[Suggestion("加索引", "低", True,
                                                                 Validation("hypopg", "verified", "6602→2.47"))])])
    out = finalreport.render_report(ev, in_, fmt="md")
    assert "# WDR 报告 — og" in out
    assert "总体状态 🟠告警" in out
    assert "判断校验 ✓ 已锚定" in out
    assert "## 维度概览" in out
    assert "## 全景分析（自顶向下）" in out
    assert "WDR_WAIT_CLASS_SKEW" in out
    assert "✅实证 6602→2.47" in out
    # ansi path renders too
    ansi = finalreport.render_report(ev, in_, fmt="ansi", no_color=True)
    assert "WDR 报告" in ansi and "┌" in ansi  # box table


def test_render_evidence_pack_sections():
    f = Finding(model.DIM_DBSTAT, "WDR_DEADLOCK", Severity.WARN, "死锁数", "6", "≥5", "e")
    dim = DimResult(dimension=model.DIM_DBSTAT, available=True, headline="死锁 6",
                    headers=["死锁"], rows=[["6"]], findings=[f])
    bad = model.degraded(model.DIM_FILEIO, "view missing")
    ev = Evidence(conn="og", window=Window(begin_id=1, end_id=2, scope="node"),
                  dims=[dim, bad], findings=[f], overall=Severity.WARN)
    out = report.render_evidence(ev)
    assert "# WDR Evidence — og" in out
    assert "## Report Window" in out
    assert "## Deterministic Findings" in out
    assert "WDR_DEADLOCK" in out
    assert "File IO：降级（view missing）" in out


def test_default_thresholds_values():
    th = thresholds.default_thresholds()
    assert th.top_sql_dbtime_notice == 30
    assert th.top_sql_dbtime_crit == 80
    assert th.temp_spill_warn == 2 << 30
    assert th.wait_skew_warn == 60


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


def test_summarize_err_keeps_the_chinese_hint_whole():
    """降级说明可以截原始报错,但 with_hint 追加的「提示:」整段要保留——GRMP 路径的报错前缀
    (status / task_id)本来就长,160 字一刀正好切在提示中间。"""
    for _m in ("model", "util"):
        sys.modules.pop(_m, None)
    sys.path.insert(0, str(_SCRIPTS))
    import util as wdr_util  # noqa: E402  —— 此刻 sys.path[0] 是 wdr 的 scripts

    hint = "当前连接的是备机(实例处于恢复态):用主库 IP 重新 gaussdb-login"
    raw = ("执行 wdr.snapshots 失败（status='failed'，task_id=grmp-67d0d417-fd23-4ad7-a689-300690a4abf2）："
           "ERROR: recovery is in progress (SQLSTATE 55000) " + "x" * 80)
    s = wdr_util.summarize_err(Exception(raw + "\n提示:" + hint))
    assert s.endswith("\n提示:" + hint)
    assert len(s) < len(raw) + len(hint)
