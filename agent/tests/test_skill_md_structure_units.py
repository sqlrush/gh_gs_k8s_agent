"""SKILL.md 的结构闸。

这 14 个文件是**交付物**，而且被脚本批量改过两次（连接指引、安全红线）。
批量改的坏法很安静：正则少匹配一个文件、插错位置、把 YAML frontmatter 顶飞、
或者把 kbimport 管理的契约块切成两半 —— 都不会让任何测试变红，只会在客户
那边表现成「某个 skill 行为跟别的不一样」。
"""
import pathlib
import re

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKILLS = sorted(_ROOT.glob("skills/*/SKILL.md"))
_AGENTS = _ROOT / "AGENTS.md"


def _frontmatter(text: str) -> dict:
    """取 YAML frontmatter。允许开头有 BOM。"""
    body = text.lstrip("﻿")
    if not body.startswith("---"):
        return {}
    end = body.find("\n---", 3)
    if end < 0:
        return {}
    return yaml.safe_load(body[3:end]) or {}


def test_every_skill_dir_has_a_skill_md():
    dirs = {p.name for p in (_ROOT / "skills").iterdir() if p.is_dir()}
    have = {p.parent.name for p in _SKILLS}
    assert dirs == have, "缺 SKILL.md 的目录：%s" % (dirs - have)


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_frontmatter_is_intact(path):
    """批量改文件最容易把 frontmatter 顶飞 —— 而 OpenCode 靠它发现 skill。

    顶飞之后 skill 直接从列表里消失，不报错。
    """
    fm = _frontmatter(path.read_text(encoding="utf-8"))
    assert fm, "%s 的 YAML frontmatter 解析不出来" % path.parent.name
    for key in ("name", "version", "description"):
        assert fm.get(key), "%s 缺 frontmatter.%s" % (path.parent.name, key)
    assert fm["name"] == path.parent.name, (
        "frontmatter.name (%s) 与目录名 (%s) 不一致 —— OpenCode 按 name 索引"
        % (fm["name"], path.parent.name))


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_security_section_is_not_duplicated(path):
    """两个「## 安全红线」的话，后一个多半是批量插入插重了，
    而读的人只会看到第一个。

    全局红线已集中到 AGENTS.md（见下），SKILL.md 里只留各 skill
    特有的红线 —— 可以没有这一节，但不能有两节。
    """
    text = path.read_text(encoding="utf-8")
    assert text.count("\n## 安全红线") <= 1, (
        "%s 的「## 安全红线」出现 %d 次"
        % (path.parent.name, text.count("\n## 安全红线")))


# AGENTS.md 是现场 agent 的全局提示词，这些短语是交付物的安全承诺。
# 计数用短语刻意取自红线的**开头**，措辞微调不至于误伤，整条删掉一定变红。
_GLOBAL_REDLINES = (
    "禁止出现明文口令",          # 明文口令红线（原先散在每个 SKILL.md）
    "credential_cli",           # 红线必须告诉用户下一步跑什么
    "禁止直接读取或者解密口令",   # 凭据文件只能经脚本解密
    "禁止提供敏感信息",          # 内置 SQL / 密钥 / 连接串不外泄
    "禁止提供接口信息",          # 端点、IP、接口路径不外泄
)


def test_global_redlines_live_in_agents_md():
    """AGENTS.md 里的「禁止行为」红线一条都不能少。

    2026-08 曾把 14 个 SKILL.md 的公共红线集中到这里去重;2026-09-10 客户安全审查 diff SKILL.md 看到红线「被删了」
    (安装脚本不拷 AGENTS.md,客户只换了 skill 目录)——于是改回**每份 SKILL.md 都带一份**,与 AGENTS.md 同一正文
    (common/red_lines.md,tools/inject_red_lines.py 注入,tests/test_red_lines_units.py 保证逐字一致)。
    这里只守 AGENTS.md 自己那几条不消失;副本一致性在 test_red_lines_units 里。
    """
    assert _AGENTS.exists(), "AGENTS.md 不存在 —— 全局安全红线没了载体"
    text = _AGENTS.read_text(encoding="utf-8")
    for phrase in _GLOBAL_REDLINES:
        n = text.count(phrase)
        assert n >= 1, "AGENTS.md 里「%s」不见了" % phrase
    copies = [p.parent.name for p in _SKILLS
              if "配置文件里绝不允许出现明文口令" not in p.read_text(encoding="utf-8")]
    assert not copies, "这些 SKILL.md 没有公共安全红线副本(跑 tools/inject_red_lines.py):%s" % copies


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_kb_contract_block_is_balanced(path):
    """契约块被切成两半的话，kbimport 下次刷新会写坏整个文件。"""
    text = path.read_text(encoding="utf-8")
    begins = text.count("KB-CONTRACT:BEGIN")
    ends = text.count("KB-CONTRACT:END")
    assert begins == ends, (
        "%s 的契约块 BEGIN=%d END=%d 不配平" % (path.parent.name, begins, ends))
    if begins:
        assert text.index("KB-CONTRACT:BEGIN") < text.index("KB-CONTRACT:END")


@pytest.mark.parametrize("path", _SKILLS, ids=lambda p: p.parent.name)
def test_no_leftover_edit_artifacts(path):
    """批量编辑残留：冲突标记、连续三个空行、重复的整段。"""
    text = path.read_text(encoding="utf-8")
    for marker in ("<<<<<<<", ">>>>>>>", "======="):
        assert marker not in text, "%s 残留冲突标记 %s" % (path.parent.name, marker)
    assert "\n\n\n\n" not in text, "%s 有连续空行，多半是插入时多带了换行" % path.parent.name


def test_data_skills_all_mention_login():
    """要连库的 skill 都得指向 gaussdb-login —— 漏掉的那个，模型会自己猜连接名。

    知识库的查询 / 导入两个 skill 都不连库，login 自己不必自指。
    """
    exempt = {"gaussdb-kb", "gaussdb-kb-import", "gaussdb-kb-init", "gaussdb-login"}
    missing = [p.parent.name for p in _SKILLS
               if p.parent.name not in exempt
               and "gaussdb-login" not in p.read_text(encoding="utf-8")]
    assert not missing, "没有指向 gaussdb-login 的 skill：%s" % missing


def test_query_kb_skill_never_offers_import_commands():
    """runtime 镜像只有查询 skill;它的 SKILL.md 不能引导模型去跑不存在的导入命令。"""
    text = (_ROOT / "skills" / "gaussdb-kb" / "SKILL.md").read_text(encoding="utf-8")
    for cmd in ("kb.py ingest", "kb.py apply", "kb.py propose", "kb.py review", "kb.py index", "kb.py setup"):
        assert cmd not in text, cmd
    assert "gaussdb-kb-import" in text


def test_agents_md_routes_import_and_query_to_different_skills():
    text = _AGENTS.read_text(encoding="utf-8")
    assert "`gaussdb-kb-import`" in text and "`gaussdb-kb`" in text


def test_data_skills_tell_the_model_to_carry_the_session_handle():
    """同沙箱多用户串库的修法靠模型把 login 发的句柄带到每一条命令上——SKILL.md 不说,模型就不会带。
    每一份写了「登录之后本 skill 不需要传 -c」的 SKILL.md 都必须紧跟句柄契约。"""
    for path in sorted(_ROOT.glob("skills/gaussdb-*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        if "登录之后本 skill" not in text:
            continue
        assert "--session" in text and "句柄" in text, path.name
        assert "不要自己挑" in text, path.name


def test_data_skills_never_offer_other_sessions():
    """客户 09-11:不带句柄时列候选 / 唯一会话自动选用 = 越权。SKILL.md 不能再有这两句。"""
    for path in sorted(_ROOT.glob("skills/gaussdb-*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        assert "只有一个会话时" not in text, path.name
        assert "转给用户确认要用哪个库" not in text, path.name
        assert "列出候选会话" not in text, path.name
