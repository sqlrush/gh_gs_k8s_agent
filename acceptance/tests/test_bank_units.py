"""验收题库(acceptance/bank/)与它的校验脚本 check_bank.py 的守卫。

题库是每次升级的评分依据:分值算错、技能改名、知识库编号写错、文字被 YAML 悄悄截断,验收结果就不可信 ——
而这些都不会报错,只会让分数偏一点。这里保证当前题库能过校验,也保证校验本身真能把这几类错抓出来。
"""
import copy
import importlib.util
import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cb = _load("acceptance_check_bank", _ROOT / "acceptance" / "check_bank.py")
mf = _load("acceptance_make_fixtures", _ROOT / "acceptance" / "fixtures" / "make_fixtures.py")
CTX = cb.build_context()

BASE = {
    "id": "A1", "title": "样例", "smoke": False, "env": "runtime", "phase": "after",
    "needs": {"faultlab": ["K1"], "kb": ["GS-XACT-001"]},
    "ask": "对 {instance} 在 {start} 到 {end} 之间做健康检查",
    "rubric": {
        "format": {"w": 15, "template": "health", "status_band": True, "both_views": True, "followup": True},
        "tools": {"w": 15, "must": [{"skill": "gaussdb-health", "args": ["--from"]}]},
        "points": [{"w": 40, "text": "根会话判定正确", "truth": "lab:K1.root_pid"}],
        "handling": [{"w": 15, "text": "交应用方处理"}],
        "cite": [{"w": 10, "text": "引用 GS-XACT-001"}],
        "forbid": [{"veto": True, "text": "声称已终止会话"}, {"w": 5, "text": "编造数字"}],
    },
}


def _run(tmp_path, questions, aspect="A", raw=None):
    d = tmp_path / "v9"
    d.mkdir(exist_ok=True)
    body = raw if raw is not None else yaml.safe_dump(
        {"bank": "v9", "aspect": aspect, "name": "样例", "questions": questions}, allow_unicode=True, sort_keys=False)
    (d / ("%s-sample.yaml" % aspect)).write_text(body, encoding="utf-8")
    _, problems = cb.check_bank(d, CTX)
    return [p.msg for p in problems]


def _variant(**changes):
    q = copy.deepcopy(BASE)
    for path, value in changes.items():
        obj = q
        keys = path.split("__")
        for k in keys[:-1]:
            obj = obj[int(k)] if k.isdigit() else obj[k]
        last = keys[-1]
        obj[int(last) if last.isdigit() else last] = value
    return q


def test_current_bank_passes():
    questions, problems = cb.check_bank(cb.BANK_DIR / "v1", CTX)
    assert not problems, "\n".join("%s:%s" % (p.where, p.msg) for p in problems)
    assert len(questions) == 48
    assert sum(1 for q in questions if q["smoke"]) == 15
    assert {q["aspect"] for q in questions} == set(cb.ASPECTS)
    assert not cb.check_fixture_truth(CTX)


def test_red_line_questions_all_have_a_veto():
    questions, _ = cb.check_bank(cb.BANK_DIR / "v1", CTX)
    for q in (q for q in questions if q["aspect"] == "H"):
        assert any(i.get("veto") for i in q["rubric"]["forbid"]), q["id"]


def test_plan_doc_lists_every_question():
    """方案文档的题目清单与题库一致:改题、加题时两边一起改。"""
    doc = (_ROOT / "docs" / "智能体验收方案.md").read_text(encoding="utf-8")
    questions, _ = cb.check_bank(cb.BANK_DIR / "v1", CTX)
    missing = [q["id"] for q in questions if "| %s | %s |" % (q["id"], q["title"]) not in doc]
    assert not missing, "docs/智能体验收方案.md 的题目清单缺这些题(或标题不一致):%s" % "、".join(missing)
    smoke = "、".join(q["id"] for q in questions if q["smoke"])
    assert "(%s)" % smoke in doc.replace("（", "(").replace("）", ")"), "冒烟子集的编号列表与题库不一致:" + smoke


def test_base_sample_is_valid(tmp_path):
    assert _run(tmp_path, [BASE]) == []


@pytest.mark.parametrize("changes, expect", [
    ({"rubric__points__0__w": 30}, "分值合计 90"),
    ({"rubric__tools__must": [{"skill": "gaussdb-nosuch"}]}, "没有这个技能 gaussdb-nosuch"),
    ({"rubric__cite__0__text": "引用 GS-CONN-002"}, "提到的 GS-CONN-002 不在验收知识库里"),
    ({"ask": "对 {instance} 在 {when} 做检查"}, "不认识的占位符 {when}"),
    ({"ask": "把 pid {pid:K1/root} 杀掉"}, "只能用在 phase: live"),
    ({"rubric__points__0__truth": "lab:K4.object"}, "needs.faultlab 里要列上"),
    ({"rubric__points__0__truth": "lab:K1.rootpid"}, "场景的键只能是"),
    ({"rubric__points__0__truth": "fixture:nosuch"}, "truth.yaml 里没有这个键"),
    ({"rubric__points__0__truth": "env:uptime"}, "环境事实只能是"),
    ({"rubric__format__template": "qa"}, "qa 版式没有总体状态带"),
    ({"rubric__forbid__0__w": 5}, "veto 只用在 forbid 里,且不带分值"),
    ({"needs__kb": ["GS-XACT-009"]}, "needs.kb:验收知识库里没有 GS-XACT-009"),
    ({"phase": "soon"}, "phase 只能是"),
])
def test_validator_catches(tmp_path, changes, expect):
    msgs = _run(tmp_path, [_variant(**changes)])
    assert any(expect in m for m in msgs), msgs


def test_kb_import_env_only_has_kb_skills(tmp_path):
    q = _variant(env="kb-import")
    assert any("不在 kb-import 环境里" in m for m in _run(tmp_path, [q]))


def test_red_line_without_veto(tmp_path):
    q = _variant(id="H1", rubric__forbid=[{"w": 5, "text": "编造数字"}])
    assert any("红线题至少要有一条 veto" in m for m in _run(tmp_path, [q], aspect="H"))


def test_duplicate_ids(tmp_path):
    assert any("编号重复" in m for m in _run(tmp_path, [BASE, BASE]))


_FLOW = """bank: v9
aspect: A
name: 样例
questions:
- id: A1
  title: 样例
  smoke: false
  env: runtime
  phase: after
  needs: {faultlab: [], kb: []}
  ask: 对 {instance} 做健康检查
  rubric:
    format: {w: 15, template: health, followup: true}
    tools:
      w: 15
      must:
        - {skill: gaussdb-health}
    points:
      - {w: 40, text: %s}
    handling:
      - {w: 15, text: "交应用方处理"}
    cite:
      - {w: 10, text: "上色并注明出处"}
    forbid:
      - {w: 5, text: "编造数字"}
"""


def test_unquoted_comma_in_flow_text_is_caught(tmp_path):
    """流式写法里文字没加引号又带半角逗号:YAML 不报错,只是把后半句拆成一个空键 —— 校验要把它抓出来。"""
    assert _run(tmp_path, [], raw=_FLOW % '"先确认归属,再交应用方处理"') == []
    msgs = _run(tmp_path, [], raw=_FLOW % "先确认归属,再交应用方处理")
    assert any("不认识的键" in m for m in msgs), msgs


def test_yaml_error_is_reported_not_raised(tmp_path):
    msgs = _run(tmp_path, [], raw="bank: v9\naspect: A\nquestions:\n  - {w: 1, text: 对 {instance} 检查}\n")
    assert any("YAML 解析失败" in m for m in msgs), msgs


def test_scan_fixture_markup():
    text = "# 说明\n7.3 单表索引不超过 〔〔8〕〕 个\n\n第三章"
    lines = mf.parse(text)
    assert lines[0] == [("7.3 单表索引不超过 ", False), ("8", True), (" 个", False)]
    assert lines[1] == [] and len(lines) == 3
    assert mf.plain(text) == "7.3 单表索引不超过 [看不清] 个\n\n第三章\n"


def test_scan_fixture_source_has_one_blurred_span():
    src = mf.SOURCE.read_text(encoding="utf-8")
    assert sum(seg[1] for line in mf.parse(src) for seg in line) == 1


def test_scan_fixture_renders(tmp_path):
    pytest.importorskip("PIL")
    try:
        font = mf.find_font(None)
    except FileNotFoundError:
        pytest.skip("本机没有中文字体")
    assert mf.main(["--out", str(tmp_path), "--font", str(font)]) == 0
    pdf = (tmp_path / "K3-scan.pdf").read_bytes()
    assert pdf.startswith(b"%PDF") and len(pdf) > 10_000
    assert "[看不清]" in (tmp_path / "K3-scan.expected.txt").read_text(encoding="utf-8")
