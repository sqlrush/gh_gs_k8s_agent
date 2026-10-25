"""交付 DML 的范围闸。

`--dml-out` 导出的每一条 INSERT，都是要灌进客户生产库 script_config 的
一条白名单 SQL。多一条没人用的，客户就得为一条没有任何 skill 验证过的
SQL 走变更评审 —— 而且是**安静地**多出来：DML 文件长几行，没人会去数。

仓库里确实有一批只给中间件自测用的脚本（perf.* 是「动态性能视图」那组
验证场景，session.* 给 grmp_demo.sh 用）。它们该留在仓库里，但不该混进
交付物。所以导出时默认拒绝，要带它们必须显式说。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from grmp_middleware import grmp_register  # noqa: E402


def test_scripts_no_skill_calls_are_listed():
    """能点出「skill 从不调用」的脚本，且认得出全名常量的写法。

    skill 里既有 run("health.overview") 这种字面量，也有
    TOP_SQL_SCRIPT = "topsql.top_sql" 再 run(TOP_SQL_SCRIPT) 的写法。
    后者必须也算「用了」，否则闸门天天误报，很快就被加参数绕过去。
    """
    unused = set(grmp_register.scripts_no_skill_calls(_ROOT / "scripts" / "registry"))

    assert "topsql.top_sql" not in unused, "常量间接引用被误判成没人用"
    assert "health.overview" not in unused, "字面量直接引用被误判成没人用"
    assert "perf.memory" in unused, "只给自测用的脚本没被点出来"
    assert "session.by_user" in unused


def test_export_refuses_to_silently_ship_unused_scripts(tmp_path, capsys):
    """默认拒绝导出，并把多余的脚本逐条列出来。

    不是「导出并警告」—— 警告在一堆输出里没人看，而 DML 已经生成好了，
    下一步就是发给客户。
    """
    out = tmp_path / "release.sql"
    rc = grmp_register.main([
        "--registry", str(_ROOT / "scripts" / "registry"),
        "--db", str(tmp_path / "s.db"),
        "--dml-out", str(out),
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert not out.exists(), "拒绝了却还是把文件写出来了"
    assert "perf.memory" in err and "session.by_user" in err


def test_export_proceeds_when_the_extra_scripts_are_asked_for(tmp_path):
    """显式要求时照导 —— 闸门是逼人做决定，不是禁止某个选择。"""
    out = tmp_path / "release.sql"
    rc = grmp_register.main([
        "--registry", str(_ROOT / "scripts" / "registry"),
        "--db", str(tmp_path / "s.db"),
        "--dml-out", str(out),
        "--include-unused",
    ])
    assert rc == 0
    assert "perf.memory" in out.read_text(encoding="utf-8")


def test_export_of_a_clean_registry_needs_no_flag(tmp_path):
    """全都有人用时不该拦 —— 否则大家会习惯性带上 --include-unused。"""
    reg = tmp_path / "registry" / "topsql"
    reg.mkdir(parents=True)
    (reg / "top_sql.yaml").write_text(
        "name: topsql.top_sql\n"
        "description: t\n"
        "readonly: true\n"
        "sql: |\n"
        "  SELECT 1 AS n\n",
        encoding="utf-8",
    )
    out = tmp_path / "release.sql"
    rc = grmp_register.main([
        "--registry", str(tmp_path / "registry"),
        "--db", str(tmp_path / "s.db"),
        "--dml-out", str(out),
    ])
    assert rc == 0
    assert out.exists()
