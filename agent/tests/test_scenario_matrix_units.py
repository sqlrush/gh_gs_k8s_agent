"""场景矩阵判定逻辑的单测 —— **给验证器做验证**。

`tools/scenario_matrix.py` 的 judge() 决定 60 个用例各自算 PASS 还是 FAIL。
它自己错了，那份「60/60 PASS」的报告就是空的 —— 而空报告比没报告更糟：
它让人以为验过了。

两个方向都要测：
  · 该 PASS 的判成 PASS —— 否则一片假红，没人再认真看
  · **该 FAIL 的必须判 FAIL** —— 这个方向更要紧
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "scenario_matrix_for_test", _ROOT / "tools" / "scenario_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sm = _load()
TRACE = "Traceback (most recent call last):\n  File ...\nValueError: boom"


# --- 铁律：Traceback 一律 BUG ------------------------------------------------

@pytest.mark.parametrize("expect", ["ok", "reject", "nocrash"])
def test_traceback_is_always_a_bug(expect):
    """**无论期望是什么。**

    未捕获异常意味着那条出错路径没人设计过，用户看到的是一段栈而不是能照做的
    提示。哪怕这条用例「本来就期望失败」，用 Traceback 失败也不合格。
    """
    verdict, note = sm.judge(expect, 1, "", TRACE)
    assert verdict == "BUG", "期望 %s 时 Traceback 也必须是 BUG" % expect


def test_traceback_on_stdout_also_counts():
    """有些脚本把栈打到 stdout —— 只看 stderr 会漏掉。"""
    assert sm.judge("ok", 0, TRACE, "")[0] == "BUG"


# --- ok：该成功的 -------------------------------------------------------------

def test_ok_passes_with_output():
    assert sm.judge("ok", 0, "| 1 | 870461000 | ...", "")[0] == "PASS"


def test_ok_fails_on_nonzero_exit():
    verdict, note = sm.judge("ok", 2, "", "error: 连不上")
    assert verdict == "FAIL" and "退出码 2" in note


def test_ok_fails_when_exit_zero_but_no_output():
    """**退出 0 却什么都没输出 = 静默失败。**

    这正是本项目一路在防的形态：命令「成功」了，但什么也没做。
    """
    assert sm.judge("ok", 0, "", "")[0] == "FAIL"
    assert sm.judge("ok", 0, "  \n ", "")[0] == "FAIL"


# --- reject：该被拒的 ---------------------------------------------------------

@pytest.mark.parametrize("blob", [
    "error: sql id not found",
    "错误：目录不存在",
    "DML keywords (INSERT/UPDATE/DELETE) detected in SQL statement.",
    "Multiple semicolons (;) detected",
    "No matching statements.",
    "未命中:'索引'",
    "需要且只能指定一个输入源",
])
def test_reject_passes_when_it_says_why(blob):
    """干净地拒 = 带明确理由。措辞各 skill 不统一，认一组片段。"""
    assert sm.judge("reject", 0, blob, "")[0] == "PASS"


def test_reject_passes_on_nonzero_exit():
    assert sm.judge("reject", 1, "", "")[0] == "PASS"


def test_reject_fails_on_silent_success():
    """**期望被拒却静默成功 —— 最危险的一种。**

    比如给了一条 DML 让 explain 拒绝，它却真出了计划：退出 0、有输出、
    没有任何错误字样。这条判不出来的话，矩阵会把一个安全缺口报成通过。
    """
    verdict, note = sm.judge("reject", 0, "Seq Scan on t  (cost=0.00..1.00)", "")
    assert verdict == "FAIL" and "静默成功" in note


# --- nocrash：只要不炸 --------------------------------------------------------

@pytest.mark.parametrize("rc,out,err", [
    (0, "结果", ""), (1, "", "error"), (2, "", ""), (-99, "", "TIMEOUT"),
])
def test_nocrash_passes_unless_traceback(rc, out, err):
    assert sm.judge("nocrash", rc, out, err)[0] == "PASS"


def test_nocrash_still_fails_on_traceback():
    assert sm.judge("nocrash", 1, "", TRACE)[0] == "BUG"


# --- 用例集本身的完整性 -------------------------------------------------------

def test_every_case_has_a_known_expectation():
    """期望值打错字（比如写成 'okk'）会让 judge 走到 reject 分支，
    于是一条本该严格检查的用例被悄悄放宽。"""
    cases = sm.build_cases("123")
    bad = [(g, n, e) for g, n, e, _a, _s in cases
           if e not in ("ok", "reject", "nocrash")]
    assert not bad, "期望值拼写错误：%s" % bad


def test_cases_cover_every_installed_skill():
    """新增 skill 后忘了加用例 —— 矩阵会「全绿」，而那个 skill 一次都没跑。"""
    cases = sm.build_cases("123")
    covered = {g for g, _n, _e, _a, _s in cases}
    expected = {"login", "topsql", "slowsql", "sqlfetch", "explain", "health",
                "sqlreview", "sqltune", "procinfo", "topproc", "proctune",
                "wdr", "memanalyze", "kb"}
    assert expected <= covered, "没有用例的 skill：%s" % (expected - covered)


def test_sqlid_dependent_cases_are_skipped_without_one():
    """取不到 sql_id 时，依赖它的用例应当不生成 —— 而不是拿空串去跑，
    那会跑出一批指向错误方向的红。"""
    with_id = {n for _g, n, _e, _a, _s in sm.build_cases("123")}
    without = {n for _g, n, _e, _a, _s in sm.build_cases("")}
    assert "有效 sql_id" in with_id and "有效 sql_id" not in without
    assert "按 sql_id" in with_id and "按 sql_id" not in without
