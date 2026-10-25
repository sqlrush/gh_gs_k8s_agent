"""没有持久会话时的降级契约。

改动前是「拿不到会话就整条停」。但 7 处取数里 5 处本来就走 runner，
EXPLAIN 模板化之后是 6 处 —— 只有 hypopg 索引验证真的要会话。
什么都不给，比给一份「带真实执行计划、索引建议标注未验证」的分析差得多。

这里钉住降级的两条底线：
  1. 索引建议**必须**带未验证标注 —— 丢了标注，降级就变成了静默降级
  2. DML + --analyze **必须**报错，不能悄悄不 analyze —— 估算计划与实际计划
     实测能差 2.3 倍，用户会拿着估算计划当实际计划用
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def _load(skill, mod):
    """按真实安装形态加载 skill 模块（同级模块要能互相 import）。"""
    import importlib.util

    path = _ROOT / "skills" / ("gaussdb-" + skill) / "scripts" / (mod + ".py")
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location("%s_%s" % (skill, mod), path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


# ===========================================================================
# 未验证标注不能丢
# ===========================================================================

class _WhitelistRunner:
    """白名单访问路径（中间件）。"""
    whitelist_only = True


class _DirectRunner:
    """直连访问路径。"""
    whitelist_only = False


@pytest.mark.parametrize("skill", ["sqltune", "proctune"])
def test_no_hypopg_note_exists_and_says_unverified(skill):
    """两个 skill 都得有这条标注，且必须点明「未经验证」。

    措辞会变，但「未经验证」这四个字是给 DBA 看的核心信号，不能改没了。
    """
    m = _load(skill, skill)
    note = getattr(m, "_NO_HYPOPG_NOTE", None)
    assert note, "%s 缺 _NO_HYPOPG_NOTE —— 降级时索引建议会不带标注" % skill
    assert "未经验证" in note
    assert "人工验证" in note, "得告诉 DBA 下一步该做什么，不能只说坏消息"


@pytest.mark.parametrize("skill", ["sqltune", "proctune"])
def test_no_hypopg_note_is_worded_per_access_path(skill):
    """「下一步该做什么」两条路不一样，标注不能只有一份措辞。

    这条原先断言 note 里必须出现直连驱动名（"得给出拿到验证背书的具体办法"）。
    在本机直连时那确实是可执行的下一步；可客户的白名单部署里**压根没有直连
    通道** —— 那正是白名单模型的前提。对着那边的 DBA 说「改用 driver: psycopg2
    重跑」，不是建议，是噪音，还会把人往「绕过白名单」的方向引。

    所以拆成两句：直连给具体办法，白名单说清这套部署里没有可切换的选项、
    以人工验证为准。两边都必须保留「未经验证 / 人工验证」这个核心信号。
    """
    m = _load(skill, skill)
    direct = m.no_hypopg_note(_DirectRunner())
    whitelist = m.no_hypopg_note(_WhitelistRunner())

    assert "psycopg2" in direct, "直连路径要给出拿到验证背书的具体办法"
    assert "psycopg2" not in whitelist, (
        "白名单路径不该提 psycopg2 —— 客户环境没有直连通道，这句话不可执行")
    for note in (direct, whitelist):
        assert "未经验证" in note and "人工验证" in note


@pytest.mark.parametrize("skill", ["sqltune", "proctune"])
def test_session_is_not_attempted_on_a_whitelist_path(skill):
    """中间件那条路注定给不了会话，就不该先试一次再从异常里恢复。

    拿 SessionUnavailable 当控制流的代价不只是白跑一趟：降级路径是靠 except
    分支拼出来的，读代码的人看不出「这条路本来就不走 hypopg」是设计，
    还是某次失败的兜底。
    """
    src = (_ROOT / "skills" / ("gaussdb-" + skill) / "scripts"
           / (skill + ".py")).read_text(encoding="utf-8")
    assert "may_provide_session" in src, (
        "%s 仍在无条件尝试 session_for()" % skill)


@pytest.mark.parametrize("skill", ["sqltune", "proctune"])
def test_degrade_path_is_taken_when_session_missing(skill):
    """db is None 时走标注分支，而不是去调 hypopg。

    直接检查源码里的分支存在 —— 这条真跑起来要一整套连接，
    而它要防的退化（有人把 if db is None 删掉）静态就能看出来。
    """
    src = (_ROOT / "skills" / ("gaussdb-" + skill) / "scripts"
           / (skill + ".py")).read_text(encoding="utf-8")
    assert "if db is None:" in src
    assert "_NO_HYPOPG_NOTE" in src


# ===========================================================================
# DML + --analyze 必须硬失败
# ===========================================================================

def test_dml_with_analyze_is_rejected_not_downgraded():
    """不能悄悄变成「不 analyze」。

    静默降级会让用户以为拿到的是实际执行的计划，而实际是估算计划。
    实测两者能差 2.3 倍（cost 1046000 vs 2448304）。
    """
    from common.grmp.statement import ExplainNotAllowed, ensure_explainable

    with pytest.raises(ExplainNotAllowed) as ei:
        ensure_explainable("UPDATE t SET a = 1", analyze=True)
    assert "只读" in str(ei.value)


# ===========================================================================
# EXPLAIN 模板的形态 —— 防护写在模板里，改坏了就没了
# ===========================================================================

_REG = _ROOT / "scripts" / "registry"


@pytest.mark.parametrize("rel", [
    "explain/plan_text.yaml",
    "sqltune/plan_text.yaml",
    "proctune/plan_text.yaml",
    "explain/plan_text_analyze.yaml",
    "sqltune/plan_text_analyze.yaml",
    "proctune/plan_text_analyze.yaml",
])
def test_explain_templates_are_read_only(rel):
    """必须标 readonly: true。

    这是三道防线里唯一由数据库强制的一道：只读会话让 DML/DDL 直接失败。
    去掉它，注入载荷就能穿透到写操作。
    """
    from common.grmp.script import load_script

    rec = load_script(_REG / rel)
    assert rec.readonly is True, "%s 不是只读脚本 —— 注入面失去数据库侧兜底" % rel


# ===========================================================================
# 模板的限制不能变成 skill 的限制 —— 实际发生过的回归
# ===========================================================================

def test_explain_never_opens_a_raw_writable_session():
    """explain 只走注册模板，**不建原始连接** —— 中间件与直连同一条路。

    这条取代了原先的 `test_template_block_falls_back_to_a_session_instead_of_failing`。
    那条钉的是源码里有没有 `template_blocked` / `connection_for` 这两个串，
    用意是保住「不带 --analyze 的 EXPLAIN UPDATE 仍能出计划」。**但那个行为
    早就没了**：main() 的 DML 形态校验先 `return 1`，回落分支根本到不了。
    实测（og5，三条访问路径）裸 UPDATE 一律 rc=1「DML keywords detected」。
    也就是说它一直在给假保证 —— 串还在，行为已经没了。

    而那条回落一旦真被触到，拿到的是 read_only=not analyze 的原始会话：
    `--analyze` 时就是**可写**会话，用户 SQL 不经 EXPLAIN 包裹直接下发。
    实测 `/* c */ UPDATE ...` 与 `/* c */ DELETE FROM ...` 由此真写了库
    （gsql 与直连驱动各复现一次，退出码 0，报告显示一份正常的执行计划）。
    所以旁路删掉了，形态校验改走 common.grmp.statement 的归一化判定。

    **遗留的产品决策**：不带 --analyze 的 DML 该不该出计划？EXPLAIN 不带
    ANALYZE 根本不执行语句，模板又是 readonly + ANALYZE 写死 false，技术上
    安全且两条模式都能做；sqlfetch 按 sql_id 取回来的 SQL 也常常就是 DML。
    今天沿用现状（拒），因为那是实测在跑的行为，改它属于加能力不属于修 bug。
    要放开的话改 shape_reject 一处即可，别再引回原始连接那条路。
    """
    src = (_ROOT / "skills" / "gaussdb-explain" / "scripts"
           / "explain.py").read_text(encoding="utf-8")
    assert "connection_for" not in src, (
        "explain 又去建原始连接了 —— 那条路 --analyze 时是可写会话，"
        "且用户 SQL 不经 EXPLAIN 包裹，实测能写库")
    assert "query_in_rollback" not in src, (
        "回滚包装实测挡不住注入（一个 `--` 就能注释掉 ROLLBACK），"
        "不该靠它兜底写操作")
    assert "for_conn" in src, "模板路径没了 —— 两条模式共用的就是这一条"


def test_explain_shape_checks_use_the_shared_normalizer():
    """形态判定必须走 common.grmp.statement，不许再在原文上跑正则。

    三个 skill 曾各抄一份 `^\\s*(insert|update|delete|merge)\\b`：`^\\s*` 跳
    空白但不跳注释，`/* c */ UPDATE ...` 判成非 DML。抄三份的直接后果是
    修一处、另两处照旧带着 bug 跑，而它们都拿这个结果决定「要不要拒」和
    「要不要包回滚」。
    """
    for rel in ("gaussdb-explain/scripts/explain.py",
                "gaussdb-proctune/scripts/evidence.py",
                "gaussdb-sqltune/scripts/evidence.py"):
        src = (_ROOT / "skills" / rel).read_text(encoding="utf-8")
        # 只看真正编译出来的正则 —— 注释里引用这个模式来说明当初错在哪，
        # 那是文档不是代码，不该把它算进来
        code = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "re.compile(r\"(?i)^\\s*(insert" not in code, (
            "%s 又抄了一份 DML 正则 —— 它不跳注释" % rel)
        assert "common.grmp.statement" in code, (
            "%s 没走共用的语句形态判定" % rel)


@pytest.mark.parametrize("rel", [
    "explain/plan_text.yaml",
    "sqltune/plan_text.yaml",
    "proctune/plan_text.yaml",
])
def test_non_analyze_templates_never_execute_user_sql(rel):
    """不带 analyze 的模板里 ANALYZE 必须写死为 false 或干脆不出现。

    写成 true 或做成参数，用户 SQL 就会被真执行 —— 那是完全不同的风险等级。
    """
    from common.grmp.script import load_script

    sql = load_script(_REG / rel).script_content.upper()
    assert "ANALYZE TRUE" not in sql, "%s 会真执行用户 SQL" % rel
    assert "{{" not in sql.split("EXPLAIN", 1)[1].split(")", 1)[0], (
        "%s 的 EXPLAIN 选项里有占位符 —— 选项不能由调用方控制" % rel)


# ===========================================================================
# Finding 不能三家各存一份 —— 汇总层要靠它跨进程解析
# ===========================================================================

@pytest.mark.parametrize("skill", ["health", "wdr", "memanalyze"])
def test_finding_is_not_redefined_per_skill(skill):
    """三个 skill 曾各存一份字段相同的 Finding。

    汇总层出现之后这不只是重复：health 解析子 skill 的 json，三份定义
    哪份多加一个字段，汇总侧读到的就和产出侧对不上 —— 而 json 对不上的
    表现往往是**少一条风险**，不是报错。
    """
    src = (_ROOT / "skills" / ("gaussdb-" + skill) / "scripts"
           / "model.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "class Finding" not in code, "%s 又自己定义了一份 Finding" % skill
    assert "from common.finding import" in code, "%s 没走共用的 Finding" % skill
