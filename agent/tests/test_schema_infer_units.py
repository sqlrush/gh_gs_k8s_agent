"""common.schema_infer —— 贴文本没带 --schema 时,按表名在目录里找 schema:唯一就用,多个候选就问,没有就照旧。

现场(客户 09-09 早):模型贴文本漏了 --schema → 400。命令是模型拼的,提示词只能约束不能保证;
脚本层自己去 pg_class 查一遍,把「模型记不记得带参数」从关键路径上拿掉。
纪律不变:同名表跨 schema 时不猜,列出候选交用户。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import schema_infer as si  # noqa: E402
from common import sqlqualify as q  # noqa: E402

SCRIPT = "sqltune.tables"


class FakeRunner:
    """按 relname 过滤的假 pg_class(nspname, relname)。"""

    def __init__(self, rows, boom=None):
        self.rows, self.boom, self.calls = rows, boom, []

    def run(self, script, values=None):
        self.calls.append((script, dict(values or {})))
        if self.boom:
            raise self.boom
        names = [x.strip().strip("'") for x in values["names"].split(",")]
        return [{"nspname": s, "relname": t} for s, t in self.rows if t in names]


def test_unqualified_tables_are_the_lookup_keys():
    assert q.unqualified_tables("select * from batch_job_status b join gmag.other o on o.id = b.id, items") == \
        ["batch_job_status", "items"]
    assert q.unqualified_tables('select * from "MixedCase" m') == ['"MixedCase"']
    assert q.unqualified_tables("select 1") == []


def test_unique_schema_is_inferred():
    r = FakeRunner([("gmag", "batch_job_status")])
    inf = si.infer(r, SCRIPT, "SELECT COUNT(1) FROM batch_job_status WHERE x = 1")
    assert inf.schema == "gmag" and not inf.ambiguous
    assert inf.tables == ("batch_job_status",)
    assert r.calls == [(SCRIPT, {"names": "'batch_job_status'"})]
    assert "唯一" in inf.reason


def test_several_tables_in_the_same_schema():
    r = FakeRunner([("gmag", "t1"), ("gmag", "t2"), ("public", "t2")])
    inf = si.infer(r, SCRIPT, "select * from t1 join t2 on t1.id = t2.id")
    assert inf.schema == "gmag" and not inf.ambiguous          # 只有 gmag 同时有两张表


def test_same_name_in_two_schemas_is_ambiguous_not_guessed():
    r = FakeRunner([("gbatch", "t_lmpcdtl"), ("dsc_ora_public", "t_lmpcdtl")])
    inf = si.infer(r, SCRIPT, "select * from t_lmpcdtl")
    assert inf.schema == "" and inf.ambiguous
    assert inf.candidates == {"t_lmpcdtl": ("dsc_ora_public", "gbatch")}
    text = si.describe(inf)
    assert "gbatch" in text and "dsc_ora_public" in text and "--schema" in text and "不猜" in text


def test_tables_spread_over_different_schemas_is_ambiguous():
    r = FakeRunner([("a", "t1"), ("b", "t2")])
    inf = si.infer(r, SCRIPT, "select * from t1, t2")
    assert inf.schema == "" and inf.ambiguous and "同一个 schema" in inf.reason


def test_guess_breaks_a_tie_only_when_it_is_a_candidate():
    r = FakeRunner([("gbatch", "t"), ("dsc_ora_public", "t")])
    assert si.infer(r, SCRIPT, "select * from t", guess="gbatch").schema == "gbatch"
    assert si.infer(r, SCRIPT, "select * from t", guess="nowhere").ambiguous


def test_missing_table_means_no_inference():
    r = FakeRunner([("gmag", "t1")])
    inf = si.infer(r, SCRIPT, "select * from t1 join t9 on true")
    assert inf.schema == "" and not inf.ambiguous and "t9" in inf.reason


def test_qualified_only_sql_needs_nothing():
    r = FakeRunner([("gmag", "t")])
    inf = si.infer(r, SCRIPT, "select * from gmag.t")
    assert inf.schema == "" and not inf.ambiguous and r.calls == []


def test_system_and_hidden_schemas_are_ignored():
    r = FakeRunner([("pg_catalog", "t"), (None, "t"), ("gmag", "t")])
    assert si.infer(r, SCRIPT, "select * from t").schema == "gmag"


def test_quoted_identifiers_are_looked_up_verbatim():
    r = FakeRunner([("gmag", "MixedCase")])
    inf = si.infer(r, SCRIPT, 'select * from "MixedCase"')
    assert inf.schema == "gmag" and r.calls[0][1]["names"] == "'MixedCase'"


def test_catalog_failure_is_reported_not_raised():
    r = FakeRunner([], boom=RuntimeError("中间件未注册脚本 sqltune.tables"))
    inf = si.infer(r, SCRIPT, "select * from t")
    assert inf.schema == "" and not inf.ambiguous and "未注册" in inf.reason
