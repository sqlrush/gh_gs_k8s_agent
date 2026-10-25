"""common.procname —— 存储过程名的解析与定位:支持 schema.package.proc 三段名,同名多个时拒绝不猜。

现场(客户 09-08 截图):模型按慢 SQL 里的写法传了 gmag.pckg_xxx.proc_xxx 三段名,旧逻辑按最后一个点切,
把 gmag.pckg_xxx 当 schema 找不到,报 not found,模型于是脑补「包子程序不在 pg_proc」。实测 openGauss/GaussDB 的
包内过程就在 pg_proc 里(propackageid → gs_package)。另一个更隐蔽的坑:不同包里的同名过程被 LIMIT 1 静默取第一个。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import procname as pn  # noqa: E402

SCRIPT = "proctune.proc_def"


def _row(schema, package, name):
    return {"nspname": schema, "proname": name, "lanname": "plpgsql", "prosrc": "BEGIN NULL; END",
            "args": "", "package": package}


class FakeRunner:
    """按 (schema, package) 过滤的假 pg_proc。"""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def run(self, script, values=None):
        v = dict(values or {})
        self.calls.append((script, v))
        return [r for r in self.rows
                if r["proname"] == v["name"]
                and (not v["schema"] or r["nspname"] == v["schema"])
                and (not v["package"] or r["package"] == v["package"])]


def test_split_one_two_three_parts():
    assert pn.split_qualified("foo") == pn.ProcRef("", "", "foo")
    assert pn.split_qualified("public.foo") == pn.ProcRef("public", "", "foo")
    assert pn.split_qualified("gmag.pckg_report.proc_x") == pn.ProcRef("gmag", "pckg_report", "proc_x")
    assert pn.split_qualified("GMAG.Pkg.Proc") == pn.ProcRef("gmag", "pkg", "proc")   # 未加引号的标识符按小写存


@pytest.mark.parametrize("bad", ["", "a.b.c.d", "a..b", ".foo", "foo.", "a b.c", "x;drop.y"])
def test_split_rejects_bad_shapes(bad):
    with pytest.raises(ValueError):
        pn.split_qualified(bad)


def test_three_part_name_filters_by_package():
    r = FakeRunner([_row("gmag", "pkg_a", "proc_x"), _row("gmag", "pkg_b", "proc_x")])
    row = pn.lookup(r, SCRIPT, "gmag.pkg_a.proc_x")
    assert row["package"] == "pkg_a"
    assert r.calls == [(SCRIPT, {"name": "proc_x", "schema": "gmag", "package": "pkg_a"})]


def test_two_part_name_tries_schema_then_package():
    """`pkg_b.proc_x`:先当 schema.proc 查(没有叫 pkg_b 的 schema),再当 package.proc 查。"""
    r = FakeRunner([_row("gmag", "pkg_a", "proc_x"), _row("gmag", "pkg_b", "proc_x")])
    row = pn.lookup(r, SCRIPT, "pkg_b.proc_x")
    assert row["nspname"] == "gmag" and row["package"] == "pkg_b"
    assert [c[1] for c in r.calls] == [
        {"name": "proc_x", "schema": "pkg_b", "package": ""},
        {"name": "proc_x", "schema": "", "package": "pkg_b"},
    ]


def test_same_name_in_two_packages_is_refused_with_candidates():
    """LIMIT 1 静默取第一个是最危险的:分析的是另一个包里的同名过程,输出看起来完全正常。"""
    r = FakeRunner([_row("gmag", "pkg_a", "proc_x"), _row("gmag", "pkg_b", "proc_x")])
    with pytest.raises(ValueError) as ei:
        pn.lookup(r, SCRIPT, "gmag.proc_x")
    msg = str(ei.value)
    assert "gmag.pkg_a.proc_x" in msg and "gmag.pkg_b.proc_x" in msg and "schema.package.proc" in msg


def test_unique_two_part_name_still_works_and_standalone_has_no_package():
    r = FakeRunner([_row("public", "", "proc_alone"), _row("gmag", "pkg_a", "proc_x")])
    assert pn.lookup(r, SCRIPT, "public.proc_alone")["package"] == ""
    assert pn.lookup(r, SCRIPT, "gmag.proc_x")["package"] == "pkg_a"   # 只有一个候选就用它


def test_not_found_message_teaches_the_three_part_form():
    r = FakeRunner([])
    with pytest.raises(ValueError) as ei:
        pn.lookup(r, SCRIPT, "gmag.pckg_report.proc_x")
    msg = str(ei.value)
    assert "gmag.pckg_report.proc_x" in msg and "pg_proc" in msg and "schema.package.proc" in msg


def test_qualified_display():
    assert pn.qualified("gmag", "pkg_a", "proc_x") == "gmag.pkg_a.proc_x"
    assert pn.qualified("gmag", "", "proc_x") == "gmag.proc_x"


def test_not_found_carries_ref_and_tried_for_the_locate_report():
    """找不到时抛 NotFound(ValueError 子类):skill 靠它上面的 ref / tried 去代为排查,而不是只打一句 error。"""
    r = FakeRunner([])
    with pytest.raises(pn.NotFound) as ei:
        pn.lookup(r, SCRIPT, "gmag.pkg_a.proc_x")
    assert isinstance(ei.value, ValueError)
    assert ei.value.ref == pn.ProcRef("gmag", "pkg_a", "proc_x")
    assert ei.value.tried == ("gmag.pkg_a.proc_x",)
    assert "找不到" in str(ei.value)
