"""根提示词与技能提示词的现场要求(2026-11-04)。

① 诊断环境与知识库导入环境各一份根提示词(AGENTS.md / AGENTS-kb-import.md,后者只在容器版的导入镜像里);
② 提示词里不出现指代行方的「客户」(「客户端」「客户表」等业务 / 技术用语除外);
③ 根提示词里不出现版本号、日期、改动标记;
④ 检索顺序:本行知识库 → 镜像里的根规范(/workspace/docs/) → 模型自带知识;根规范模板不得引用。
"""
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROOT_PROMPTS = [p for p in (_ROOT / "AGENTS.md", _ROOT / "AGENTS-kb-import.md") if p.exists()]
PROMPTS = ROOT_PROMPTS + sorted(_ROOT.glob("skills/gaussdb-*/SKILL.md")) + sorted(_ROOT.glob("skills/gaussdb-*/references/*.md")) \
    + sorted(_ROOT.glob("common/answer_templates/*.md")) + [_ROOT / "common" / "red_lines.md"]
BUSINESS_TERMS = ("客户端", "客户表", "客户信息", "客户号", "客户经理", "客户编号", "客户名称", "客户数据模型")
VERSION_MARKS = re.compile(r"▼▼▼|▲▲▲|skills-v\d|agent-v\d|\bv\d+\.\d+(\.\d+)?\b|20\d\d-\d\d-\d\d")


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: str(p.relative_to(_ROOT)))
def test_prompts_do_not_call_the_bank_a_customer(path):
    text = path.read_text(encoding="utf-8")
    for w in BUSINESS_TERMS:
        text = text.replace(w, "")
    hits = [l.strip()[:60] for l in text.splitlines() if "客户" in l]
    assert not hits, "提示词里还有「客户」:%s" % hits[:3]


@pytest.mark.parametrize("path", ROOT_PROMPTS, ids=lambda p: p.name)
def test_root_prompts_carry_no_version_or_change_marks(path):
    hits = [l.strip()[:60] for l in path.read_text(encoding="utf-8").splitlines() if VERSION_MARKS.search(l)]
    assert not hits, "根提示词里还有版本 / 日期 / 改动标记:%s" % hits[:3]


@pytest.mark.parametrize("path", ROOT_PROMPTS, ids=lambda p: p.name)
def test_retrieval_order_is_kb_then_root_specs_then_model(path):
    text = path.read_text(encoding="utf-8")
    assert "本行知识库 → 镜像里的根规范 → 模型自带知识" in text
    kb, spec, fallback = (text.index("kb.py query"), text.index("python3 /workspace/search_docs.py"),
                          text.index("知识库中未找到相关内容,以下为通用知识,仅供参考".replace(",", "，")))
    assert kb < spec < fallback, "顺序:知识库 → 根规范 → 兜底"
    assert "不是规范，不得引用" in text, "根规范模板不能被当成规范引用"
    assert "引用自《文档名称》第X章/X节" in text and "依据《文档名称》" not in text, "根规范出处与知识库同一种括号写法"


def test_import_prompt_is_about_importing_and_keeps_every_prohibition():
    kbi = (_ROOT / "AGENTS-kb-import.md")
    if not kbi.exists():
        pytest.skip("只有容器版有导入环境的根提示词")
    text = kbi.read_text(encoding="utf-8")
    assert "知识库导入环境" in text and "不含数据库诊断" in text
    for skill in ("gaussdb-kb-init", "gaussdb-kb-import", "gaussdb-kb"):
        assert "`%s`" % skill in text
    diag = [s for s in ("gaussdb-health", "gaussdb-sqltune", "gaussdb-lockwait", "gaussdb-wdr", "gaussdb-login") if s in text]
    assert not diag, "导入环境没有诊断技能,根提示词不该引导去用:%s" % diag
    runtime = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in ("禁止创建或修改 Skill", "禁止执行危险命令", "禁止访问外网", "禁止出现明文口令", "禁止直接读取或者解密口令",
                   "禁止提供敏感信息", "禁止提供接口信息", "输出屏蔽规则"):
        assert phrase in text and phrase in runtime, "两份根提示词的禁止项一条都不能少:%s" % phrase
