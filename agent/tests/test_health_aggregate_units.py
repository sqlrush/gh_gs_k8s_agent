"""子进程汇总模块的单元测试。全部用假 runner 注入，不启动真实子进程、不连库。"""
import json
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-health" / "scripts"))

import aggregate  # noqa: E402
from common.finding import Finding, Severity, findings_to_json  # noqa: E402


def _finding(**kw):
    base = dict(dimension="lockwait", code="LW001", severity=Severity.WARN,
                metric="wait_s", value="12.0", threshold="10.0",
                evidence="pid 100 等待 pid 200")
    base.update(kw)
    return Finding(**base)


class _Completed:
    """假的 subprocess.CompletedProcess，避免真的起子进程。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake(rc=0, out="", err=""):
    """返回一个记录了每次调用 argv/kwargs 的假 runner。"""
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Completed(returncode=rc, stdout=out, stderr=err)

    runner.calls = calls
    return runner


def _timeout_runner(timeout=30):
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", timeout))
    return runner


def test_script_path_is_a_sibling_layout():
    """仓库里与安装后是同一结构：skills/gaussdb-X/scripts/X.py"""
    p = aggregate.script_path("gaussdb-lockwait")
    assert p.parts[-3:] == ("gaussdb-lockwait", "scripts", "lockwait.py")


def test_script_path_covers_every_sub_skill():
    """三个子 skill 各自的脚本名都是「去掉 gaussdb- 前缀 + .py」。"""
    expect = {
        "gaussdb-lockwait": "lockwait.py",
        "gaussdb-waitevent": "waitevent.py",
        "gaussdb-vacuum": "vacuum.py",
    }
    for skill, filename in expect.items():
        p = aggregate.script_path(skill)
        assert p.name == filename
        assert p.parent.name == "scripts"
        assert p.parent.parent.name == skill


def test_sub_skills_and_needs_target_are_the_documented_sets():
    assert aggregate.SUB_SKILLS == ("gaussdb-lockwait", "gaussdb-waitevent", "gaussdb-vacuum")
    assert aggregate.NEEDS_TARGET == (
        "gaussdb-explain", "gaussdb-sqltune", "gaussdb-sqlreview",
        "gaussdb-sqlfetch", "gaussdb-proctune",
    )
    # 两个集合不重叠：能跑的和不能跑的不能是同一个 skill。
    assert not set(aggregate.SUB_SKILLS) & set(aggregate.NEEDS_TARGET)


def test_success_parses_findings():
    payload = findings_to_json([_finding()], skill="gaussdb-lockwait")
    r = aggregate.run_sub_skill("gaussdb-lockwait", "og", 30, runner=_fake(rc=0, out=payload))
    assert r.ok is True
    assert r.error == ""
    assert len(r.findings) == 1
    assert r.findings[0].code == "LW001"
    assert r.skill == "gaussdb-lockwait"


def test_success_with_no_findings_is_ok_true_empty_list():
    """真正「没查出风险」和「查失败」必须能区分：这是前者。"""
    payload = findings_to_json([], skill="gaussdb-vacuum")
    r = aggregate.run_sub_skill("gaussdb-vacuum", "og", 30, runner=_fake(rc=0, out=payload))
    assert r.ok is True
    assert r.findings == []
    assert r.error == ""


def test_nonzero_exit_is_recorded_not_raised():
    """子 skill 失败不能掀翻 health，但必须留下原因。"""
    r = aggregate.run_sub_skill(
        "gaussdb-lockwait", "og", 30,
        runner=_fake(rc=2, err="error: 连不上\n更多堆栈细节在这里"))
    assert r.ok is False
    assert "连不上" in r.error
    # 只取首行，堆栈细节不该泄漏进汇总报告
    assert "更多堆栈细节" not in r.error
    assert r.findings == []


def test_nonzero_exit_reason_skips_leading_notices():
    """首行是提示不是错误时,要跳过它取真正的原因。

    GRMP 路径上子 skill 会先打一行「注意:…--timeout 30 不会生效」,再打真正的报错。
    只取首行的结果是:三个子技能全失败,报告与大盘上写的失败原因却是一句超时提示,
    真正的「连接被关闭」一个字都看不到。2026-09-23 集群全链路测试抓到。
    """
    err = ("注意：连接 x 的 driver 是 grmp，该访问路径无法设置语句超时（协议没有这个参数），--timeout 30 不会生效。\n"
           "[warn] 报告未存档:x\n"
           "error: 请求中间件失败：Remote end closed connection without response\n"
           "更多堆栈")
    r = aggregate.run_sub_skill("gaussdb-lockwait", "og", 30, runner=_fake(rc=1, err=err))
    assert "Remote end closed" in r.error, r.error
    assert "不会生效" not in r.error


def test_nonzero_exit_with_only_notices_still_has_a_reason():
    r = aggregate.run_sub_skill("gaussdb-lockwait", "og", 30,
                                runner=_fake(rc=1, err="注意：只有一条提示\n"))
    assert r.error, "只有提示时也不能留空"


def test_nonzero_exit_with_empty_stderr_still_has_a_reason():
    """stderr 是空的也不能让 error 是空字符串——空字符串会被当成「没出错」。

    stdout 特意给一份*合法*的 findings json：如果实现忘了检查 returncode，
    这里就会被当成解析成功、ok 变成 True——用这个来专门盯住「有没有检查
    returncode」这一步，不要跟「stdout 解析失败」的失败路径混在一起。
    """
    valid_stdout = findings_to_json([], skill="gaussdb-vacuum")
    r = aggregate.run_sub_skill("gaussdb-vacuum", "og", 30,
                                 runner=_fake(rc=1, out=valid_stdout, err=""))
    assert r.ok is False
    assert r.error != ""
    assert "解析" not in r.error  # 失败原因该来自 returncode，不是巧合的解析失败
    assert r.findings == []


def test_timeout_is_recorded_as_a_failure():
    """超时也是失败，不是「没风险」。"""
    r = aggregate.run_sub_skill("gaussdb-waitevent", "og", 30, runner=_timeout_runner(30))
    assert r.ok is False
    assert "超时" in r.error
    assert r.findings == []


def test_unparseable_stdout_is_a_failure_not_an_empty_list():
    """**json 解析不出来 ≠ 没风险。** 返回空列表会让 health 报「这块没问题」。"""
    r = aggregate.run_sub_skill("gaussdb-lockwait", "og", 30, runner=_fake(rc=0, out="not json"))
    assert r.ok is False
    assert "解析" in r.error
    assert r.findings == []


def test_malformed_findings_shape_is_a_failure_not_an_empty_list():
    """json 能解析，但形状不对（缺字段）——同样是失败，不是空列表。"""
    bad = '{"skill": "gaussdb-vacuum", "findings": [{"dimension": "vacuum"}]}'
    r = aggregate.run_sub_skill("gaussdb-vacuum", "og", 30, runner=_fake(rc=0, out=bad))
    assert r.ok is False
    assert "解析" in r.error
    assert r.findings == []


def test_null_severity_is_a_failure_not_a_crash():
    """所有必填字段都在（缺字段检查通不过不了这条），但 severity 是 null。

    common/finding.py 里 `Severity(int(raw["severity"]))`，`int(None)` 抛的
    是 **TypeError**，不是 `ValueError`。run_sub_skill 如果只接住
    ValueError，这条 TypeError 会直接冒出去，砸穿「绝不 raise」这条模块
    存在的理由——而且是最糟的版本：形状对的了（字段都全乎），值错了，
    照样得是 ok=False，不能变成一个真的 Traceback。
    """
    bad = json.dumps({"skill": "gaussdb-vacuum", "findings": [{
        "dimension": "vacuum", "code": "V001", "severity": None,
        "metric": "dead_tuples", "value": "1", "threshold": "1",
        "evidence": "x",
    }]})
    r = aggregate.run_sub_skill("gaussdb-vacuum", "og", 30, runner=_fake(rc=0, out=bad))
    assert r.ok is False
    assert "解析" in r.error
    assert r.findings == []


def test_severity_value_that_overflows_int_is_a_failure_not_a_crash():
    """severity 是一个合法的 JSON 数字，但大到 int() 转不了。

    `1e400` 是合法的 JSON number 语法；`json.loads` 对超出 float 范围的
    数字走 IEEE-754 的 string-to-double，直接饱和成 `inf`，不报错。
    `common/finding.py` 的 `Severity(int(raw["severity"]))` 对 `inf` 调用
    `int()` 抛的是 **OverflowError**（`ArithmeticError` 的子类）——
    既不是 `ValueError` 也不是 `TypeError`。这是三轮走查里第三种被
    发现的异常类型，也是促使把 `run_sub_skill` 的解析异常处理从「挨个
    枚举」改成「在这个信任边界上 except Exception」的那个案例：
    proc.stdout 是不受控子进程吐出来的文本，没理由相信我们已经想全了
    它能坏成什么样。
    """
    bad = ('{"skill": "gaussdb-vacuum", "findings": [{"dimension": "vacuum", '
           '"code": "V001", "severity": 1e400, "metric": "dead_tuples", '
           '"value": "1", "threshold": "1", "evidence": "x"}]}')
    r = aggregate.run_sub_skill("gaussdb-vacuum", "og", 30, runner=_fake(rc=0, out=bad))
    assert r.ok is False
    assert "解析" in r.error
    # 错误文案里必须带着异常类型名——不然以后 findings_from_json 自己出个
    # 编程错误（比如 AttributeError），也会被这条路径误标成「子 skill
    # 数据不对」，把排查方向指错。
    assert "OverflowError" in r.error
    assert r.findings == []


def test_runner_raising_unexpectedly_is_recorded_not_raised():
    """subprocess 起不来（脚本路径错、权限问题……）也不能掀翻 health。"""
    def runner(argv, **kwargs):
        raise FileNotFoundError("no such file or directory")

    r = aggregate.run_sub_skill("gaussdb-lockwait", "og", 30, runner=runner)
    assert r.ok is False
    assert r.error != ""
    assert r.findings == []


def test_conn_and_format_are_passed_through():
    """子进程必须用同一个连接、同一种输出格式、同一个超时值，否则查的是
    别的库/解析不了/子 skill 用了自己的默认超时（也就是没超时）。"""
    runner = _fake(rc=0, out=findings_to_json([], skill="gaussdb-vacuum"))
    aggregate.run_sub_skill("gaussdb-vacuum", "og-grmp", 45, runner=runner)
    assert len(runner.calls) == 1
    argv, _kwargs = runner.calls[0]
    assert "-c" in argv and argv[argv.index("-c") + 1] == "og-grmp"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    # --timeout 必须真的转发给子进程，值必须是调用方传进来的那个 timeout；
    # 丢了这一项，子 skill 会悄悄用回自己的默认值（通常是不设超时）。
    assert "--timeout" in argv and argv[argv.index("--timeout") + 1] == str(45)
    assert str(aggregate.script_path("gaussdb-vacuum")) in argv


def test_environment_is_inherited_for_the_token():
    """中间件令牌在环境变量里——不继承的话子进程一律鉴权失败。"""
    runner = _fake(rc=0, out=findings_to_json([], skill="gaussdb-vacuum"))
    aggregate.run_sub_skill("gaussdb-vacuum", "og-grmp", 30, runner=runner)
    _argv, kwargs = runner.calls[0]
    assert "env" not in kwargs


def test_sub_skill_result_is_frozen():
    r = aggregate.SubSkillResult(skill="gaussdb-vacuum", ok=True, findings=[], error="")
    try:
        r.ok = False
    except Exception as exc:
        assert "frozen" in str(type(exc).__name__).lower() or "FrozenInstance" in str(exc) \
            or isinstance(exc, Exception)
    else:
        raise AssertionError("SubSkillResult 应该是不可变的（frozen dataclass）")


def test_collect_all_runs_every_sub_skill_with_the_same_conn_and_timeout():
    seen = []

    def fake_run_sub_skill(skill, conn, timeout, runner=None):
        seen.append((skill, conn, timeout))
        return aggregate.SubSkillResult(skill=skill, ok=True, findings=[], error="")

    real = aggregate.run_sub_skill
    aggregate.run_sub_skill = fake_run_sub_skill
    try:
        results = aggregate.collect_all("og-grmp", 30)
    finally:
        aggregate.run_sub_skill = real

    assert [r.skill for r in results] == list(aggregate.SUB_SKILLS)
    assert seen == [(s, "og-grmp", 30) for s in aggregate.SUB_SKILLS]


def test_collect_all_keeps_going_after_one_sub_skill_fails():
    """一个子 skill 挂了不能拖累其它两个——三个都要跑完。"""
    def fake_run_sub_skill(skill, conn, timeout, runner=None):
        if skill == "gaussdb-lockwait":
            return aggregate.SubSkillResult(skill=skill, ok=False, findings=[], error="炸了")
        return aggregate.SubSkillResult(skill=skill, ok=True, findings=[], error="")

    real = aggregate.run_sub_skill
    aggregate.run_sub_skill = fake_run_sub_skill
    try:
        results = aggregate.collect_all("og", 30)
    finally:
        aggregate.run_sub_skill = real

    by_skill = {r.skill: r for r in results}
    assert len(results) == 3
    assert by_skill["gaussdb-lockwait"].ok is False
    assert by_skill["gaussdb-waitevent"].ok is True
    assert by_skill["gaussdb-vacuum"].ok is True
