"""health 交出 waits/bloat/lwlock/locks 四个维度，改为汇总三个子 skill 的风险。

**这个文件钉的最重要的一件事**：aggregate.SubSkillResult 的 ok 字段和
findings 是否为空列表是两件独立的事——

    失败：ok=False, findings=[]
    干净：ok=True,  findings=[]

两者 findings 都是空列表，唯一的区别是 ok。任何渲染/判断代码如果改成看
`len(findings)` 而不是看 `ok`，一个崩溃的子 skill 就会被读成「没查出风险」，
Task 18/19 想防的静默失效原样发生。本文件的
test_a_clean_sub_skill_is_not_confused_with_a_failed_one_by_list_length
专门盯这一点。

DB-free：全程不连库，不起子进程，用假 runner / 假 aggregate 结果注入。
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-health" / "scripts"
_REG = _ROOT / "scripts" / "registry"

# health / wdr / memanalyze 都有同名模块（model/report/collectors/...）。
# 不清缓存的话，谁先跑谁的版本就赢，测试顺序一变结果就变。
for _m in ("model", "thresholds", "util", "collectors", "report", "health",
           "aggregate", "render"):
    sys.modules.pop(_m, None)
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_ROOT))

import aggregate  # noqa: E402
import collectors  # noqa: E402
import health  # noqa: E402
import report  # noqa: E402
from common import access  # noqa: E402
from common.finding import Finding, Severity  # noqa: E402
from model import HealthEvidence  # noqa: E402
from thresholds import default_thresholds  # noqa: E402

# 捕获在模块加载时（此时 sys.modules["thresholds"] 确定是 health 自己的）。
# 之后即便别的测试文件（wdr/waitevent/vacuum 都有同名 thresholds.py）
# 重新 purge 并导入了它们自己的版本，这个已绑定的名字不受影响——它是
# 当时那个模块对象的引用，不会跟着 sys.modules 里的新绑定漂移。测试函数体
# 内绝不能再临时 `from thresholds import ...`：那才会在执行阶段（而不是
# collection 阶段）重新查 sys.modules，谁的测试文件后收集、后 purge，
# 就会把这个通用名字污染成谁的版本。


def _section(text: str, title: str) -> str:
    """取出某个 '## title' 到下一个 '## ' 之间的正文，方便按段断言。"""
    marker = "## " + title
    assert marker in text, "报告里没有 %r 这一节" % marker
    start = text.index(marker) + len(marker)
    rest = text[start:]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


def _finding(**kw):
    base = dict(dimension="锁等待", code="LW001", severity=Severity.WARN,
                metric="wait_s", value="12.0", threshold="10.0",
                evidence="pid 100 等待 pid 200", skill="gaussdb-lockwait")
    base.update(kw)
    return Finding(**base)


# --- Step 1：四个维度不再本地采集 --------------------------------------------


def test_the_four_dimensions_are_no_longer_collected_locally():
    """交出去了就不能还留一份 —— 两份采集会给出不一致的数字，且不一致是静默的。"""
    src = (_SCRIPTS / "collectors.py").read_text(encoding="utf-8")
    for gone in ("health.lock_chain", "health.waits", "health.lwlock", "health.bloat"):
        assert gone not in src, "collectors.py 还在自己查 %s" % gone


def test_the_other_local_dimensions_are_untouched():
    """交接任务唯一该动的是那 4 个维度；本地维度一个都不能少。

    storage 是后来（2026-10-17）新增的一维，不是这次交接动的 ——
    列在这里是为了让"少了一维"依然能被这条测试抓住。
    """
    keys = [k for k, _ in collectors.registry()]
    assert keys == ["overview", "slowsql", "xact", "conn", "logs", "repl",
                    "schema", "storage", "concurrency"]
    for gone in ("waits", "bloat", "lwlock", "locks"):
        assert gone not in keys


def test_the_retired_registry_scripts_are_gone():
    for rel in ("lock_chain.yaml", "waits.yaml", "lwlock.yaml", "bloat.yaml"):
        assert not (_REG / "health" / rel).exists(), "%s 该删了" % rel


# --- Step 2：ok，不是 len(findings) ------------------------------------------


def test_failed_sub_skill_is_named_in_the_report():
    """**不静默跳过。** 少一节的报告和干净的报告不能长得一样。"""
    bad = aggregate.SubSkillResult(
        skill="gaussdb-lockwait", ok=False, findings=[],
        error="子进程启动失败：No such file or directory")
    ev = HealthEvidence(conn="og")
    out = report.render_health(ev, sub_results=[bad])
    missing = _section(out, "本次未采集到的维度")
    assert "gaussdb-lockwait" in missing
    assert "No such file or directory" in missing


def test_a_clean_sub_skill_is_not_confused_with_a_failed_one_by_list_length():
    """两个 SubSkillResult 的 findings 都是空列表，唯一区别是 ok。

    如果渲染代码哪天改成 `if not r.findings`（看长度）而不是 `if not r.ok`，
    这条测试必须变红：ok=True 的干净结果会被误判成失败，出现在
    「本次未采集到的维度」里，把「没查出风险」念成「没查到」。
    """
    clean = aggregate.SubSkillResult(skill="gaussdb-vacuum", ok=True,
                                     findings=[], error="")
    failed = aggregate.SubSkillResult(skill="gaussdb-lockwait", ok=False,
                                      findings=[], error="超时（30s）")
    ev = HealthEvidence(conn="og")
    out = report.render_health(ev, sub_results=[clean, failed])

    missing = _section(out, "本次未采集到的维度")
    assert "gaussdb-lockwait" in missing
    assert "超时（30s）" in missing
    # 干净的（ok=True, findings=[]）绝不能出现在「未采集到」这一节里——
    # 出现了就说明判断代码看的是 len(findings) 而不是 ok。
    assert "gaussdb-vacuum" not in missing


def test_all_ok_sub_skills_report_as_fully_collected():
    """三个都 ok=True 时（哪怕都没查出风险），顶部这段不能列出任何失败——
    允许在"全部成功"的汇报文字里提到 skill 名，但不能出现失败列表的
    项目符号（"- **skill**：原因"），也不能有 error 文案。"""
    results = [aggregate.SubSkillResult(skill=s, ok=True, findings=[], error="")
              for s in aggregate.SUB_SKILLS]
    ev = HealthEvidence(conn="og")
    out = report.render_health(ev, sub_results=results)
    missing = _section(out, "本次未采集到的维度")
    assert "全部采集成功" in missing
    for line in missing.splitlines():
        assert not line.strip().startswith("- **"), (
            "ok=True 的子 skill 不该出现在失败列表的项目符号里：%r" % line)


def test_an_empty_scope_is_said_out_loud_not_left_blank():
    """`--include overview` / `--exclude locks,waits,lwlock,bloat` 之后一个子
    skill 都不在范围内——这一段必须**明说**是范围排除掉的，不能留白。

    留白的后果与 ok/len(findings) 混淆是同一类：一份没有锁、等待、膨胀信息
    的报告，和一份"这三块都查过、都干净"的报告长得一模一样。区别只在这句
    话在不在。这条分支此前是 `_missing_section()` 三个分支里唯一没有测试
    盯着的一个（退出码那条 test_exit_code_0_when_nothing_was_in_scope 只管
    rc，不看报告说了什么）。
    """
    ev = HealthEvidence(conn="og")
    out = report.render_health(ev, sub_results=[])
    missing = _section(out, "本次未采集到的维度")
    assert "没有子 skill 纳入范围" in missing, (
        "一个子 skill 都不在范围内时，这一段必须说清楚是被 --include/--exclude "
        "排除的，否则读者无法把它与「三块都查过且干净」区分开：%r" % missing)
    assert "全部采集成功" not in missing, (
        "一个都没跑，不能说成「全部采集成功」——这正好把「没查」念成「查过没事」")


# --- Step 3：未纳入汇总的能力 --------------------------------------------


def test_skills_needing_a_target_are_listed_as_not_covered():
    ev = HealthEvidence(conn="og")
    out = report.render_health(ev, sub_results=[])
    section = _section(out, "未纳入汇总的能力")
    for skill in aggregate.NEEDS_TARGET:
        assert skill in section, "%s 没有被点名未覆盖" % skill


def test_needs_target_section_survives_even_with_no_sub_results_info():
    """这一段说的是 health 结构性覆盖不到什么，跟这次子 skill 跑没跑成无关，
    永远都该出现——不能因为调用方没传 sub_results 就消失。"""
    ev = HealthEvidence(conn="og")
    out = report.render_health(ev)  # 不传 sub_results，走默认值
    assert "## 未纳入汇总的能力" in out


# --- findings 带来源 ----------------------------------------------------


def test_sub_skill_findings_are_annotated_with_their_source():
    """子 skill 产出的 finding 混进主表后，得让人知道详情去哪查。"""
    f = _finding(skill="gaussdb-lockwait")
    ev = HealthEvidence(conn="og", findings=[f], overall=Severity.WARN)
    out = report.render_health(ev, sub_results=[])
    assert "（详见 gaussdb-lockwait）" in out


def test_locally_collected_findings_are_not_falsely_annotated():
    """本地 8 个维度产的 finding 没有 skill 来源，不该被安一个假来源。"""
    f = _finding(skill="", dimension="Overview", code="CACHE_LOW")
    ev = HealthEvidence(conn="og", findings=[f], overall=Severity.WARN)
    out = report.render_health(ev, sub_results=[])
    assert "（详见" not in out


# --- --include/--exclude 的四个老维度名要继续认得 -----------------------


def test_legacy_dimension_names_still_route_to_the_right_sub_skill():
    assert health._sub_skill_in_scope("gaussdb-lockwait", {"locks"}, set())
    assert not health._sub_skill_in_scope("gaussdb-waitevent", {"locks"}, set())
    assert not health._sub_skill_in_scope("gaussdb-vacuum", {"locks"}, set())


def test_waits_and_lwlock_both_route_to_waitevent():
    """waits/lwlock 是同一份子报告(gaussdb-waitevent)产出的，拆不开来源，
    include 任一个都该触发它。"""
    assert health._sub_skill_in_scope("gaussdb-waitevent", {"waits"}, set())
    assert health._sub_skill_in_scope("gaussdb-waitevent", {"lwlock"}, set())


def test_exclude_any_of_waits_or_lwlock_skips_the_whole_waitevent_report():
    assert not health._sub_skill_in_scope("gaussdb-waitevent", set(), {"waits"})
    assert not health._sub_skill_in_scope("gaussdb-waitevent", set(), {"lwlock"})


def test_bloat_routes_to_vacuum():
    assert health._sub_skill_in_scope("gaussdb-vacuum", {"bloat"}, set())


def test_no_include_or_exclude_means_every_sub_skill_is_in_scope():
    for s in aggregate.SUB_SKILLS:
        assert health._sub_skill_in_scope(s, set(), set())


# --- --include/--exclude 接口稳定性：四个老名字不能悄悄失效 ---------------


def test_all_four_legacy_dimension_names_stay_accepted_and_correctly_routed():
    """brief 的硬要求：locks/waits/lwlock/bloat 这四个名字在 --include/
    --exclude 里必须继续有效，用户已有的命令行不能因为这次内部重组而失效。

    这条测试防的是"其余测试全绿，只有这个命令行接口静默失效"——比如以后
    有人从 SUB_SKILL_DIMS 里删掉了 "locks": ("locks",) 这一条（或者把它的
    元组改空），本文件前面几条分散的路由测试未必会全部覆盖到这个删除，
    这里用一个显式的期望表把四个名字一次性钉死，任何一个从接受集合里
    掉出去都必须让本测试变红。
    """
    expect_owner = {
        "locks": "gaussdb-lockwait",
        "waits": "gaussdb-waitevent",
        "lwlock": "gaussdb-waitevent",
        "bloat": "gaussdb-vacuum",
    }
    for name, owner in expect_owner.items():
        assert health._sub_skill_in_scope(owner, {name}, set()), (
            "--include %s 应该触发 %s，但没有——这个老维度名可能从接受"
            "集合里掉出去了" % (name, owner))
        for other in aggregate.SUB_SKILLS:
            if other != owner:
                assert not health._sub_skill_in_scope(other, {name}, set()), (
                    "--include %s 不该触发 %s" % (name, other))
        assert not health._sub_skill_in_scope(owner, set(), {name}), (
            "--exclude %s 应该排除 %s，但没有" % (name, owner))


# --- 退出码 ---------------------------------------------------------------


def test_exit_code_3_when_a_dimension_failed():
    """一份缺了锁和等待的体检报告若退出 0，脚本里与干净报告无法区分。"""
    results = [aggregate.SubSkillResult(skill="gaussdb-lockwait", ok=False,
                                        findings=[], error="超时（30s）"),
               aggregate.SubSkillResult(skill="gaussdb-waitevent", ok=True,
                                        findings=[], error=""),
               aggregate.SubSkillResult(skill="gaussdb-vacuum", ok=True,
                                        findings=[], error="")]
    assert health._exit_code(results) == 3


def test_exit_code_0_when_everything_collected():
    results = [aggregate.SubSkillResult(skill=s, ok=True, findings=[], error="")
              for s in aggregate.SUB_SKILLS]
    assert health._exit_code(results) == 0


def test_exit_code_0_when_nothing_was_in_scope():
    """用户用 --exclude 把三个子 skill 全排除掉，没有失败可言。"""
    assert health._exit_code([]) == 0


def test_main_still_prints_the_report_on_exit_code_3(monkeypatch, capsys):
    """**报告照常打印**——3 是附加信息，不是替代输出。"""
    class _FakeRunner:
        def run(self, name, params=None):
            raise access.QueryError("db unreachable（测试用假连接）")

    def _fake_for_conn(conn, timeout=None):
        return _FakeRunner()

    def _fake_collect_all(conn, timeout):
        return [
            aggregate.SubSkillResult(skill="gaussdb-lockwait", ok=False,
                                     findings=[], error="超时（30s）"),
            aggregate.SubSkillResult(skill="gaussdb-waitevent", ok=True,
                                     findings=[], error=""),
            aggregate.SubSkillResult(skill="gaussdb-vacuum", ok=True,
                                     findings=[], error=""),
        ]

    monkeypatch.setattr(health.access, "for_conn", _fake_for_conn)
    monkeypatch.setattr(health.aggregate, "collect_all", _fake_collect_all)

    rc = health.main(["-c", "fake"])
    out = capsys.readouterr().out
    assert rc == 3
    assert "# Health Evidence" in out
    assert "gaussdb-lockwait" in out
    assert "超时（30s）" in out


def test_main_returns_0_when_every_sub_skill_succeeds(monkeypatch, capsys):
    class _FakeRunner:
        def run(self, name, params=None):
            raise access.QueryError("db unreachable（测试用假连接）")

    def _fake_for_conn(conn, timeout=None):
        return _FakeRunner()

    def _fake_collect_all(conn, timeout):
        return [aggregate.SubSkillResult(skill=s, ok=True, findings=[], error="")
               for s in aggregate.SUB_SKILLS]

    monkeypatch.setattr(health.access, "for_conn", _fake_for_conn)
    monkeypatch.setattr(health.aggregate, "collect_all", _fake_collect_all)

    rc = health.main(["-c", "fake"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# Health Evidence" in out


# --- run_health 把子 skill 的 findings 并进 ev.findings ------------------


def test_run_health_merges_ok_sub_skill_findings():
    class _FakeRunner:
        def run(self, name, params=None):
            raise access.QueryError("skip local collectors")

    f = _finding(skill="gaussdb-lockwait")
    results = [aggregate.SubSkillResult(skill="gaussdb-lockwait", ok=True,
                                        findings=[f], error="")]
    ev = health.run_health(_FakeRunner(), [], [], 10, default_thresholds(),
                           sub_results=results)
    assert f in ev.findings


def test_run_health_does_not_merge_findings_from_a_failed_sub_skill():
    """ok=False 时 findings 本来就该是空列表（aggregate 的契约），
    这里再确认一遍 run_health 不会凭空造出风险。"""
    class _FakeRunner:
        def run(self, name, params=None):
            raise access.QueryError("skip local collectors")

    results = [aggregate.SubSkillResult(skill="gaussdb-lockwait", ok=False,
                                        findings=[], error="炸了")]
    ev = health.run_health(_FakeRunner(), [], [], 10, default_thresholds(),
                           sub_results=results)
    assert ev.findings == []


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except TypeError:
            # 需要 pytest fixture（monkeypatch/capsys）的用例在直跑模式下跳过
            print(f"skip  {fn.__name__}（需要 pytest fixture）")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
