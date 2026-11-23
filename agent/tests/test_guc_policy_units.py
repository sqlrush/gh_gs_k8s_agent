"""参数允许清单(common/guc_policy.py)的单测与守卫。

2026-11-05 行方要求诊断站在应用 / 运维 DBA 角度:只有非常典型的场景才建议调内核参数。允许清单只有一份,
这里核对三件事:识别规则本身;安全红线里列的参数与清单一致;全部技能的输出文字与文档里**建议修改**的参数都合规
(清单外的不许建议,清单内的要按本类写法写)—— 以后谁在脚本或文档里加回一句「调大 shared_buffers」,这里就红。
"""
import ast
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import guc_policy as gp  # noqa: E402

# 识别成「建议」但其实是描述状态或引用行方案例的句子:逐条说明为什么不算,新增要写理由
EXEMPT = {
}


def test_categories_do_not_overlap_and_names_are_known():
    cats = [set(gp.TYPICAL), set(gp.SCOPED), set(gp.OBSERVABILITY), set(gp.BASELINE)]
    for i, a in enumerate(cats):
        for b in cats[i + 1:]:
            assert not a & b
    assert not set(gp.CATEGORY) & set(gp.NOT_ADVISED)


@pytest.mark.parametrize("text,expect", [
    ("处置:调大 random_page_cost 到 1.1", [("random_page_cost", "不在")]),
    ("不要调大 query_dop", []),                                   # 禁止句不是建议
    ("SET statement_timeout = 5000", []),                        # 工具自己会话里的语句
    ("需运维评估后调大 autovacuum_max_workers", []),
    ("调大 autovacuum_max_workers", [("autovacuum_max_workers", "A 类")]),
    ("只对这条 SQL 在会话级 SET work_mem='64MB'", []),
    ("调大 work_mem", [("work_mem", "B 类")]),
    ("需运维开启 enable_stmt_track 后才能取到", []),
    ("max_connections 当前 1000,已用 990", []),                   # 只读:没有修改动词
    ("表 t_order 的表级 autovacuum_vacuum_scale_factor 调小", []),
    ("限速的表在表级调小 autovacuum_vacuum_cost_delay", []),          # autovacuum_* 表级存储参数属 B 类
    ("调小 autovacuum_vacuum_cost_delay", [("autovacuum_vacuum_cost_delay", "不在")]),
])
def test_recognition(text, expect):
    got = [(n, why) for n, _s, why in gp.violations(text)]
    assert len(got) == len(expect)
    for (n, why), (en, ew) in zip(got, expect):
        assert n == en and ew in why


def test_red_lines_name_exactly_the_typical_parameters():
    text = (_ROOT / "common" / "red_lines.md").read_text(encoding="utf-8")
    clause = text[text.index("站在应用 DBA / 运维 DBA 的角度"):]
    for name in gp.TYPICAL:
        assert "`%s`" % name in clause, "安全红线没列出 A 类参数 %s" % name
    for name in ("work_mem", "lockwait_timeout"):
        assert "`%s`" % name in clause
    named = {n for n in gp.KNOWN if "`%s`" % n in clause}
    assert named <= set(gp.TYPICAL) | set(gp.SCOPED), "红线里点名了清单外的参数:%s" % (named - set(gp.TYPICAL) - set(gp.SCOPED))


def _texts(path: pathlib.Path):
    """一个文件里会给到人(或模型)看的文字:.py 取字符串常量,.md 逐行。"""
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".md":
        return list(enumerate(raw.splitlines(), 1))
    tree = ast.parse(raw)
    docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    out = []
    for node in ast.walk(tree):            # 文档字符串是写给开发者的,不进输出,不扫
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.append((node.lineno, node.value))
    return out


def _sources():
    skip = {"tests", "testdata", "grmp_middleware", "tools", "__pycache__"}
    for p in sorted(_ROOT.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".md") and not skip & set(p.relative_to(_ROOT).parts) \
                and p.name != "guc_policy.py":
            yield p


def test_no_skill_text_recommends_parameters_outside_the_policy():
    bad, used = [], set()
    for p in _sources():
        rel = p.relative_to(_ROOT).as_posix()
        for line, text in _texts(p):
            for name, sentence, why in gp.violations(text):
                if (rel, name) in EXEMPT:
                    used.add((rel, name))
                    continue
                bad.append("%s:%d %s —— %s:%s" % (rel, line, name, why, sentence[:60]))
    assert not bad, "这些地方建议了允许清单外的参数,或没按本类写法写(common/guc_policy.py):\n" + "\n".join(bad)
    assert used == set(EXEMPT), "豁免里有已经不存在的条目:%s" % (set(EXEMPT) - used)
