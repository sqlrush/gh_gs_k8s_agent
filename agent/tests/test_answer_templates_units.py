"""回答模板(common/answer_templates/)的守卫:每份副本与正文逐字一致、每个技能归了类、旧写法清干净。

2026-10-26 回答模板化(需求 docs/design/2026-10-26-answer-template-requirements.md):健康检查、故障排查、SQL 优化、知识问答、
WDR、榜单各有固定的段落。正文只有一份,由 tools/inject_answer_templates.py 按技能所属类型拼好注入 —— 改了正文忘了注入、
哪份被手工改坏、新加的技能忘了归类,都在这里变红。
"""
import pathlib
import re
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from tools import inject_answer_templates as at  # noqa: E402

_PART_DIR = _ROOT / at.CANON_DIR
# 知识库契约的正文:gh_skill 在 gaussdb-kb 下,容器线(gh_agent_k8s)在 gaussdb-kb-import 下
_CONTRACT = next(p for p in (_ROOT / "skills" / s / "references" / "kb-contract.md" for s in ("gaussdb-kb", "gaussdb-kb-import"))
                 if p.exists())
_AGENTS = _ROOT / "AGENTS.md"


def test_every_skill_is_either_templated_or_deliberately_not():
    dirs = {p.name for p in (_ROOT / "skills").glob("gaussdb-*") if p.is_dir()}
    covered = set(at.SKILL_PARTS) | set(at.UNTEMPLATED)
    # 两个仓库共用一份注入表:容器线多一个 gaussdb-kb-import,所以只要求「每个技能都登记了」
    assert dirs <= covered, "新加的技能要在 SKILL_PARTS 或 UNTEMPLATED 里登记:%s" % (dirs - covered)
    assert set(at.SKILL_PARTS) <= dirs, "SKILL_PARTS 里有不存在的技能:%s" % (set(at.SKILL_PARTS) - dirs)
    assert not set(at.SKILL_PARTS) & set(at.UNTEMPLATED)


def test_every_part_is_used_and_exists():
    used = {p for parts in at.SKILL_PARTS.values() for p in parts} | set(at.AGENTS_PARTS)
    have = {p.stem for p in _PART_DIR.glob("*.md")}
    assert used == have, "common/answer_templates/ 里的文件与注入表不一致:%s" % (used ^ have)


def test_injector_is_in_sync():
    assert at.check(_ROOT) == [], "改了 common/answer_templates/ 后跑 python3 tools/inject_answer_templates.py"
    proc = subprocess.run([sys.executable, str(_ROOT / "tools" / "inject_answer_templates.py"), "--check", "--root", str(_ROOT)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("name", sorted(at.SKILL_PARTS))
def test_skill_md_carries_its_template_inside_its_own_section(name):
    text = (_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert text.count(at.BEGIN) == 1 and text.count(at.END) == 1
    assert text.count("\n" + at.SECTION + "\n") == 1, "%s 的「## 回答模板」要恰好一节" % name
    assert text.index("\n" + at.SECTION + "\n") < text.index(at.BEGIN) < text.index("\n## 安全红线")
    assert at.extract_block(text) == at.render(at.SKILL_PARTS[name])
    outside = text.replace(at.extract_block(text), "")
    assert "「## 回答模板」" in outside, "%s 的报告一步要指向回答模板,不然模板只是摆在那里" % name


@pytest.mark.parametrize("name", at.UNTEMPLATED)
def test_untemplated_skills_stay_clean(name):
    md = _ROOT / "skills" / name / "SKILL.md"
    if not md.exists():
        pytest.skip("本仓库没有 %s" % name)
    assert at.BEGIN not in md.read_text(encoding="utf-8")


def test_agents_md_carries_routing_and_cross_skill_parts_before_output_format():
    text = _AGENTS.read_text(encoding="utf-8")
    assert text.count(at.BEGIN) == 1
    assert at.extract_block(text) == at.render(at.AGENTS_PARTS)
    assert text.index(at.BEGIN) < text.index("\n## 输出格式要求")


def test_agents_md_dropped_the_formats_that_fight_the_template():
    """D17:客户原有的引用格式、兜底说法、诊断 / 问答输出与新模板冲突,同一份提示词里两套格式,模型会随机挑一套。"""
    text = _AGENTS.read_text(encoding="utf-8")
    for stale in ("依据《文档名称》", "以下内容未在本地规范文档中找到", "〔类型 ID〕", "- 故障现象描述", "- 直接回答问题"):
        assert stale not in text, "AGENTS.md 还留着旧写法:%s" % stale
    # SQL 审查保持客户原有格式(D18)
    assert "### SQL 审查输出" in text and "- 问题列表（逐项说明）" in text


def test_citation_rules_have_one_wording_in_contract_and_template():
    """知识引用写法:带契约的 9 个技能从契约里读,知识库技能与 AGENTS.md 从模板里读 —— 两处必须是同一段原文。"""
    cite = at.part("cite")
    assert cite in _CONTRACT.read_text(encoding="utf-8"), "kb-contract.md 里的「知识引用写法」与 common/answer_templates/cite.md 不一致"
    assert "cite" in at.SKILL_PARTS["gaussdb-kb"] and "cite" in at.AGENTS_PARTS


def test_one_fallback_phrase_everywhere():
    """以前两种兜底说法并存(AGENTS.md 一种、契约一种);统一后不许再出现旧的。"""
    files = [_AGENTS, _CONTRACT] + sorted((_ROOT / "skills").glob("gaussdb-*/SKILL.md"))
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "本行无先例,以下为通用做法" not in text and "本行无先例，以下为通用做法" not in text, f
    assert "知识库中未找到相关内容,以下为通用知识,仅供参考" in at.part("cite")


def test_common_part_says_how_to_write_for_both_readers():
    common = at.part("common")
    for must in ("应用视角:", "数据库机制:", "同一件事、同一批对象", "需要就 xxx 展开说明吗", "提议不能替代正文", "不下调"):
        assert must in common, must


@pytest.mark.parametrize("name,heads", [
    ("health", ("① 现象", "② 根因和证据链", "③ 解决方案", "④ 总结和结论")),
    ("fault", ("① 现象", "② 根因", "③ 解决方案", "④ 总结", "最多 3 个")),
    ("sqltune", ("① 现状与趋势", "② SQL 文本和执行计划解读", "③ 怎么优化", "需实测", "不拿 cost 倍数换算", "④ 总结")),
    ("qa", ("解答来源", "① 解答正文", "② 举例说明", "不编造「本行出现过」", "③ 引申和拓展")),
    ("wdr", ("① 负载画像", "② 问题与元凶", "③ 解决方案", "④ 总结和结论", "附录", "kbRefs")),
    ("ranking", ("① 榜单", "② 为什么排上来", "③ 建议下一步")),
])
def test_each_template_keeps_its_sections_in_order(name, heads):
    text = at.part(name)
    pos = [text.index(h) for h in heads]
    assert pos == sorted(pos), "%s 的段落顺序不对" % name


def test_related_symptoms_cover_the_common_complaints():
    table = at.part("related")
    rows = [l for l in table.splitlines() if l.startswith("| ") and not l.startswith("| 用户报的现象")]
    assert len(rows) >= 10
    for complaint in ("交易超时", "锁等待", "死锁", "CPU 高", "临时文件", "连接数满", "表膨胀", "内存高"):
        assert complaint in table, complaint


def test_injector_updates_a_stale_copy_and_is_idempotent(tmp_path):
    root = tmp_path
    (root / at.CANON_DIR).mkdir(parents=True)
    for p in {p for parts in at.SKILL_PARTS.values() for p in parts} | set(at.AGENTS_PARTS):
        (root / at.CANON_DIR / ("%s.md" % p)).write_text("### %s\n\n正文 %s\n" % (p, p), encoding="utf-8")
    sk = root / "skills" / "gaussdb-health"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: gaussdb-health\n---\n\n# H\n\n## 工作流\n\n1. x\n\n## 安全红线\n\n- y\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# agent\n\n## 输出格式要求\n\n### 输出屏蔽规则\n", encoding="utf-8")
    assert at.check(root) != []
    at.inject(root)
    assert at.check(root) == []
    text = (sk / "SKILL.md").read_text(encoding="utf-8")
    assert text.index("## 回答模板") < text.index("## 安全红线") and "正文 health" in text
    before = text
    at.inject(root)
    assert (sk / "SKILL.md").read_text(encoding="utf-8") == before
    (root / at.CANON_DIR / "health.md").write_text("### health\n\n改过的正文\n", encoding="utf-8")
    assert at.check(root) == ["skills/gaussdb-health/SKILL.md"]


def test_template_tags_are_copyable_verbatim():
    """颜色标签照抄才变色:不带引号、# 前没有空格(2026-10-24 测试环境实测,写成中文引号整段不变色)。"""
    cite = at.part("cite")
    for tag in re.findall(r"<font[^>]*>", cite):
        assert '"' not in tag and "'" not in tag and "= " not in tag and " #" not in tag, tag
    assert "<font color=#2f6fdb>" in cite and "<font color=#0f7c86>" in cite
