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
    gui = [l.strip()[:60] for l in text.splitlines() if "贵行" in l]
    assert not gui, "提示词里还有「贵行」(一律写本行):%s" % gui[:3]


@pytest.mark.parametrize("path", ROOT_PROMPTS, ids=lambda p: p.name)
def test_root_prompts_carry_no_version_or_change_marks(path):
    hits = [l.strip()[:60] for l in path.read_text(encoding="utf-8").splitlines() if VERSION_MARKS.search(l)]
    assert not hits, "根提示词里还有版本 / 日期 / 改动标记:%s" % hits[:3]


@pytest.mark.parametrize("path", ROOT_PROMPTS, ids=lambda p: p.name)
def test_retrieval_order_is_kb_then_root_specs_then_model(path):
    text = path.read_text(encoding="utf-8")
    assert "本行知识库 → 镜像里的根规范 → 模型自带知识" in text
    kb, spec, fallback = (text.index("kb.py query"), text.index("python3 $GSDB_ROOTSPEC_HOME/search_docs.py"),
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
    for phrase in ("禁止创建或修改 Skill", "禁止执行危险命令", "禁止访问外网", "配置文件里绝不允许出现明文口令", "禁止直接读取或者解密口令",
                   "强制拒绝机制", "绝对沉默条款", "输出屏蔽规则",
                   "显示更多、完整信息", "访问数据库的接口地址或相对路径", "禁止读取配置文件显示这些敏感信息"):
        assert phrase in text and phrase in runtime, "两份根提示词的禁止项一条都不能少:%s" % phrase


@pytest.mark.parametrize("path", ROOT_PROMPTS, ids=lambda p: p.name)
def test_red_lines_sit_inside_the_prohibitions(path):
    """安全红线并入「禁止行为」:不再单独成一节,红线块在「### 禁止行为」与「### 允许行为」之间。"""
    text = path.read_text(encoding="utf-8")
    assert "## 安全红线" not in text
    begin = text.index("<!-- RED-LINES:BEGIN")
    assert text.index("\n### 禁止行为\n") < begin < text.index("<!-- RED-LINES:END -->") < text.index("\n### 允许行为\n")


def test_runtime_prompt_leaves_report_generation_to_the_front_end():
    """「帮我生成报告」以后由行方自己的前端调用,根提示词里不再教模型怎么出报告、给链接。"""
    text = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "帮我生成报告" not in text and "[打开报告]" not in text


ENV_PATHS = re.compile(r"/data/|/workspace|/nas/|/opt/agent|/run/|/home/")


@pytest.mark.parametrize("path", ROOT_PROMPTS, ids=lambda p: p.name)
def test_root_prompts_name_paths_by_environment_variable(path):
    """根提示词里不写测试环境的绝对路径(/data/config/…、/workspace/…、/nas/kb),写环境变量;变量在容器里设好、权限放行变量写法。"""
    text = path.read_text(encoding="utf-8")
    hits = [l.strip()[:60] for l in text.splitlines() if ENV_PATHS.search(l)]
    assert not hits, "根提示词里还有环境的绝对路径:%s" % hits[:3]
    for var in ("$GSDB_SKILLS_DIR", "$GSDB_KB_DIR", "$GSDB_ROOTSPEC_HOME"):
        assert var in text, var
    assert "read 工具不展开变量" in text


# 系统 SQL:查系统表 / 系统视图 / 数据字典、调系统函数的语句(只认真正的语句形态,正文里提到表名不算)
_SYSOBJ = r"(?:pg_catalog\.)?(?:pg_\w+|gs_\w+|dbe_perf\.\w+|information_schema\.\w+|adm_\w+|db_\w+|my_\w+)"
SYSTEM_SQL = re.compile(r"(?i)\bselect\b[^。；\n`]{0,160}?\bfrom\s+" + _SYSOBJ
                        + r"|\bselect\s+(?:pg_|gs_)\w+\s*\(|\bgrant\b[^。；\n]{0,80}\bon\s+(?:pg_catalog\.)?(?:pg_|gs_|adm_)\w+")
PROMPT_DOCS = PROMPTS + sorted(_ROOT.glob("skills/gaussdb-*/references/*.md"))


@pytest.mark.parametrize("path", sorted(set(PROMPT_DOCS)), ids=lambda p: str(p.relative_to(_ROOT)))
def test_prompts_carry_no_system_sql(path):
    """2026-11-04:回答里不出现内核自己的 SQL 与查数据字典的 SQL —— 提示词里写了,模型就会照抄。处置写要做的事。"""
    hits = [l.strip()[:80] for l in path.read_text(encoding="utf-8").splitlines() if SYSTEM_SQL.search(l)]
    assert not hits, "提示词里有系统 SQL:%s" % hits[:3]


def test_red_lines_forbid_system_sql_in_answers():
    text = (_ROOT / "common" / "red_lines.md").read_text(encoding="utf-8")
    assert "不写系统 SQL" in text and "只列业务 SQL" in text and "不写语句" in text


@pytest.mark.parametrize("line,is_sql", [
    ("由人工 `SELECT pg_terminate_backend(<pid>);`", True), ("SELECT * FROM pg_locks WHERE granted", True),
    ("select text from adm_source where name = ?", True), ("`GRANT SELECT ON pg_catalog.adm_source TO <账号>;`", True),
    ("SELECT n.nspname FROM pg_namespace n JOIN pg_class c ON c.relnamespace = n.oid", True),
    ("此刻模式读 `pg_locks` / `pg_stat_activity`", False), ("本案发生时 pg_locks 无等待", False),
    ("不执行 `ALTER SYSTEM` / `SET` / `gs_guc`", False), ("SELECT * FROM fl.orders WHERE id = ?", False),
])
def test_the_system_sql_guard_can_actually_fail(line, is_sql):
    """守卫本身要能红:真语句认得出,正文里提到系统表名不误报。"""
    assert bool(SYSTEM_SQL.search(line)) is is_sql, line
