"""白名单核对:解析交付 DML、两种已登记读法、六类比对结果。"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from release import whitelist as wl  # noqa: E402

DELIVERY = _ROOT / "docs" / "delivery"
COLS = ('id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, '
        'execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, '
        'create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid')


def _insert(name, body, params='[]', scene="AGENT"):
    return ("INSERT INTO grmp.script_config (%s) VALUES (grmp.script_config_seq.nextval, 'SQL', '%s', 'postgres', 1, "
            "'ALL', NULL, NULL, NULL, 'centralization', '%s', '%s', '%s', 1, '999999999', now(), '999999999', NULL, 0, "
            "NULL, 'ALL', uuid());\n" % (COLS, name, body.replace("'", "''"), params, scene))


def test_the_shipped_full_dml_parses_to_132_scripts():
    scripts = wl.parse_dml((DELIVERY / "scripts.sql").read_text(encoding="utf-8"))
    assert len(scripts) == 132         # v1.4.13:新增 2、8 条两段 SQL 的脚本不再交付
    assert all(re.match(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", n) for n in scripts)
    # 业务库类的脚本参数最前面是路由参数 dbName(行方写法 String,2026-11-11):核对时一并比,能看出行方导没导本版增量
    assert scripts["procinfo.source_adm"].params == (("dbName", "String"), ("name", "STRING"))


def test_md5_matches_the_self_checks_shipped_in_the_delta_files():
    """增量文件末尾的自检 SQL 写着「库里 md5(script_content) 应等于这个值」—— 拿它当标准答案,口径必须一模一样。"""
    # 同一条脚本可能在几份增量里先后更新过:以**最新**那份写的为准(更早的那份描述的是当时的正文)
    scripts = wl.parse_dml((DELIVERY / "scripts.sql").read_text(encoding="utf-8"))
    latest = {}
    for f in sorted(DELIVERY.glob("scripts-delta-*.sql"), reverse=True):
        for name, md5 in re.findall(r"\('([a-z_]+\.[a-z_0-9]+)', '([0-9a-f]{32})'\)", f.read_text(encoding="utf-8")):
            latest.setdefault(name, md5)
    assert len(latest) >= 12
    for name, md5 in latest.items():
        if name not in scripts:          # 早期增量里有、本版不再交付的(v1.4.13 起 8 条两段 SQL 的脚本)
            continue
        assert scripts[name].md5 == md5, name


def test_quotes_newlines_and_parentheses_inside_the_body_survive():
    body = "SELECT '(x)', 'it''s'\nFROM t WHERE a = '{{name}}';\n"
    s = wl.parse_dml(_insert("t.one", body, '[{"key":"name","value":"","type":"STRING","autoAcquire":false}]'))
    import hashlib
    assert s["t.one"].md5 == hashlib.md5(body.encode()).hexdigest()
    assert s["t.one"].params == (("name", "STRING"),)


def test_other_scenes_are_ignored_and_a_duplicate_in_the_delivery_file_is_an_error():
    assert set(wl.parse_dml(_insert("a.b", "x\n") + _insert("c.d", "y\n", scene="OTHER"))) == {"a.b"}
    with pytest.raises(wl.WhitelistError, match="两次"):
        wl.parse_dml(_insert("a.b", "x\n") + _insert("a.b", "x\n"))


def _expected():
    return wl.parse_dml(_insert("a.ok", "1\n") + _insert("a.missing", "2\n") + _insert("a.content", "3\n")
                        + _insert("a.params", "4\n", '[{"key":"k","type":"INTEGER"}]') + _insert("a.dup", "5\n")
                        + _insert("a.invalid", "6\n"))


def test_compare_names_every_kind_of_problem():
    exp = _expected()
    md5 = {n: s.md5 for n, s in exp.items()}
    reg = wl.from_rows([
        ("a.ok", md5["a.ok"], "[]", 1),
        ("a.content", "0" * 32, "[]", 1),
        ("a.params", md5["a.params"], '[{"key":"k","value":"9","type":"STRING","autoAcquire":true}]', 1),
        ("a.dup", md5["a.dup"], "[]", 1), ("a.dup", md5["a.dup"], "[]", 1),
        ("a.invalid", md5["a.invalid"], "[]", 0),
        ("z.extra", "x", "[]", 1),
    ])
    rep = wl.compare(exp, reg)
    got = {f.name: f.status for f in rep.findings}
    assert got == {"a.ok": wl.OK, "a.missing": wl.MISSING, "a.content": wl.CONTENT, "a.params": wl.PARAMS,
                   "a.dup": wl.DUPLICATE, "a.invalid": wl.INVALID}
    assert not rep.passed and rep.extra == ["z.extra"], "多出来的只列出,不算失败"
    assert rep.counts()[wl.MISSING] == 1


def test_value_and_autoacquire_do_not_count_as_a_param_change():
    exp = wl.parse_dml(_insert("a.p", "1\n", '[{"key":"k","value":"","type":"INTEGER","autoAcquire":false}]'))
    reg = wl.from_rows([("a.p", exp["a.p"].md5, '[{"key":"k","value":"30","type":"INTEGER","autoAcquire":true}]', "1")])
    assert wl.compare(exp, reg).passed


def test_export_csv_with_comma_or_tab():
    exp = _expected()
    for sep in (",", "\t"):
        lines = [sep.join(["script_name", "content_md5", "parameter_config", "is_valid"])]
        for n, s in exp.items():
            params = '"[{""key"":""k"",""type"":""INTEGER""}]"' if n == "a.params" else "[]"
            if n != "a.dup":
                lines.append(sep.join([n, s.md5, params, "1"]))
        lines.append(sep.join(["a.dup", exp["a.dup"].md5, "[]", "1"]))
        rep = wl.compare(exp, wl.from_csv("\n".join(lines) + "\n"))
        assert rep.passed, (sep, rep.problems)


def test_a_csv_without_the_expected_header_says_how_to_export():
    with pytest.raises(wl.WhitelistError, match="SELECT"):
        wl.from_csv("name,sum\na.b,x\n")


def test_json_round_trip_for_release_yaml():
    exp = _expected()
    assert wl.from_json(wl.to_json(exp)) == exp
