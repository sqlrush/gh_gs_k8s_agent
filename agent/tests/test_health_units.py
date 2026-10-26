"""DB-free unit tests for the health skill (thresholds, severity, rendering).

health is multi-module with cross-imports (collectors -> model/thresholds/util),
so we put its scripts dir on sys.path and import by real module names to keep a
single shared copy of each module.
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"

# health and wdr both define modules named model/thresholds/util/collectors/report
# (with different contents). Purge any cached copies so this file always loads
# health's, regardless of test collection order. Captured module objects below
# stay self-consistent even if a later test file re-purges and loads wdr's.
for _m in ("model", "thresholds", "util", "collectors", "report",
           "native", "snaps", "interp", "recheck", "finalreport", "kbrefs", "appendix", "mdreport", "render", "ansi"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import collectors  # noqa: E402
import model  # noqa: E402
import report  # noqa: E402
import thresholds  # noqa: E402
import util  # noqa: E402
from model import DimResult, Finding, HealthEvidence, Severity  # noqa: E402


def test_registry_has_the_local_collectors_in_order():
    """waits/bloat/lwlock/locks moved to sub-skills (aggregate.py); the rest
    stay local, in this order — the order is the report's section order.

    storage 是 2026-10-17 新增的一维（库/表空间占用量）。它排在 schema 之后、
    concurrency 之前：对象体检完接着看这些对象占了多少地方，是读报告的自然顺序。
    """
    keys = [k for k, _ in collectors.registry()]
    assert keys == ["overview", "slowsql", "xact", "conn", "logs", "repl",
                    "schema", "storage", "concurrency"]


def test_go_duration_matches_go():
    f = thresholds.go_duration
    assert f(5) == "5s"
    assert f(30) == "30s"
    assert f(120) == "2m0s"
    assert f(300) == "5m0s"
    assert f(600) == "10m0s"
    assert f(1800) == "30m0s"
    assert f(7200) == "2h0m0s"
    assert f(0) == "0s"


def test_severity_worst_and_label():
    assert model.worst([]) == Severity.OK
    assert model.worst([Severity.NOTICE, Severity.WARN, Severity.OK]) == Severity.WARN
    assert model.worst([Severity.WARN, Severity.CRITICAL]) == Severity.CRITICAL
    assert Severity.CRITICAL.label() == "🔴严重"
    assert Severity.WARN.label() == "🟠告警"
    assert Severity.NOTICE.label() == "🟡关注"
    assert Severity.OK.label() == "🟢健康"


def test_sev_by_duration_bands():
    # notice=300 warn=1800 crit=7200 (long-xact defaults)
    assert util.sev_by_duration(100, 300, 1800, 7200) == Severity.OK
    assert util.sev_by_duration(400, 300, 1800, 7200) == Severity.NOTICE
    assert util.sev_by_duration(2000, 300, 1800, 7200) == Severity.WARN
    assert util.sev_by_duration(8000, 300, 1800, 7200) == Severity.CRITICAL
    # crit=0 disables the critical band
    assert util.sev_by_duration(10 ** 9, 300, 1800, 0) == Severity.WARN


def test_escalate_caps_at_critical():
    assert util.escalate(Severity.OK) == Severity.NOTICE
    assert util.escalate(Severity.WARN) == Severity.CRITICAL
    assert util.escalate(Severity.CRITICAL) == Severity.CRITICAL


def test_human_bytes():
    assert util.human_bytes(512) == "512B"
    assert util.human_bytes(2 * 1024) == "2.0K"
    assert util.human_bytes(16 << 20) == "16.0M"
    assert util.human_bytes(3 << 30) == "3.0G"


def test_xact_threshold_uses_go_duration():
    th = thresholds.default_thresholds()
    assert collectors._xact_threshold("XACT_LONG", Severity.NOTICE, th) == ">5m0s"
    assert collectors._xact_threshold("XACT_LONG", Severity.CRITICAL, th) == ">2h0m0s"
    assert collectors._xact_threshold("XACT_IDLE", Severity.WARN, th) == ">10m0s"


def test_default_thresholds_values():
    th = thresholds.default_thresholds()
    assert th.slow_sql_avg_ms == 500
    assert th.slow_sql_low_cpu_ratio == 0.10
    assert th.repl_lag_notice == 16 << 20
    assert th.dead_tup_min == 100000


def test_render_health_sections_and_status():
    f = Finding("Dead Tuples & Bloat", "BLOAT_DEAD_RATIO", Severity.WARN,
                "t dead_ratio", "66.67%", ">40% 且 dead>100000", "evidence")
    dim = DimResult(dimension="Dead Tuples & Bloat", available=True,
                    headline="t dead 67%", headers=["table", "dead%"],
                    rows=[["t", "66.67"]], findings=[f])
    bad = model.degraded("Replication / Standby", "view missing")
    ev = HealthEvidence(conn="og", dims=[dim, bad], findings=[f], overall=Severity.WARN)
    out = report.render_health(ev)
    assert "# Health Evidence — og" in out
    assert "总体状态：🟠告警" in out
    assert "## Deterministic Findings" in out
    assert "BLOAT_DEAD_RATIO" in out
    assert "## Collection Notes" in out
    assert "Replication / Standby：降级（view missing）" in out


def test_render_health_empty_findings():
    dim = DimResult(dimension="Overview", available=True, headline="ok",
                    headers=["a"], rows=[["1"]])
    ev = HealthEvidence(conn="og", dims=[dim], findings=[], overall=Severity.OK)
    out = report.render_health(ev)
    assert "无（所有维度未越阈值）。" in out
    assert "全部维度采集成功。" in out


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
    """降级说明可以截原始报错,但不能把「提示:」那段切掉——GRMP 路径的报错前缀
    (status / task_id)本来就长,一刀 160 字正好切在提示中间,客户看到的是半句话。"""
    # 不写 `from util import`:wdr / memanalyze 也有同名 util 模块,收集顺序不同会拿到别家的副本。
    # collectors 是本文件顶部锁定的 health 副本,它引用的 summarize_err 就是 health 的。
    summarize_err = collectors.summarize_err

    raw = ("执行 health.slow_sql 失败（status='failed'，task_id=grmp-67d0d417-fd23-4ad7-a689-300690a4abf2）："
           "ERROR: permission denied for schema dbe_perf (SQLSTATE 42501) " + "x" * 80)
    hint = "执行账号没有 schema dbe_perf 的访问权限:请 DBA 给执行账号授予该 schema 的 USAGE 权限"
    s = summarize_err(Exception(raw + "\n提示:" + hint))
    assert s.endswith("\n提示:" + hint)            # 提示整段保留
    head = s[: -len("\n提示:" + hint)]
    assert head.endswith("…") and len(head) <= 161  # 原文部分照旧截断
    assert summarize_err(Exception("短报错")) == "短报错"
