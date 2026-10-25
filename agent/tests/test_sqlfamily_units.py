"""DB-free unit tests for the gaussdb-slowsql/gaussdb-topsql/gaussdb-sqlfetch/gaussdb-explain skills."""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _dir(skill: str) -> str:
    """技能目录一律带 gaussdb- 前缀；调用方两种写法都收。

    这里曾经写死 skills/<skill>/，目录名却是 skills/gaussdb-<skill>/ ——
    收集阶段就 FileNotFoundError，整个 tests/ 一起中断。也就是说本文件
    自客户版导入起从未跑过，看着像覆盖，实际一条没执行。
    """
    return skill if skill.startswith("gaussdb-") else "gaussdb-" + skill


def _load(skill: str, mod: str):
    path = _ROOT / "skills" / _dir(skill) / "scripts" / f"{mod}.py"
    # Ensure the skill's scripts dir is importable for its sibling `render`.
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(f"{skill}_{mod}", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(m)
    return m


sqlfetch = _load("sqlfetch", "sqlfetch")
explain = _load("explain", "explain")
slowsql = _load("slowsql", "slowsql")
topsql = _load("topsql", "topsql")


def test_sqlfetch_count_placeholders():
    assert sqlfetch.count_placeholders("a = ? and b = $1 and c = :name") == 3
    assert sqlfetch.count_placeholders("x::int") == 0


def test_sqlfetch_truncation_detection():
    # Complete statements -> not truncated.
    assert sqlfetch.looks_truncated("SELECT 1")[0] is False
    assert sqlfetch.looks_truncated("SELECT a FROM t WHERE x IN (1,2,3)")[0] is False
    # openGauss-cut tails -> truncated.
    assert sqlfetch.looks_truncated("SELECT a FROM t WHERE p IN (\n  SELECT")[0] is True  # unbalanced paren
    assert sqlfetch.looks_truncated("SELECT a, b,")[0] is True                            # trailing comma
    assert sqlfetch.looks_truncated("SELECT a FROM t WHERE x =")[0] is False              # '=' not in tail set
    assert sqlfetch.looks_truncated("SELECT a FROM t ORDER BY")[0] is True                # dangling keyword
    # A paren inside a string literal must not trip the balance check.
    assert sqlfetch.looks_truncated("SELECT '(' AS x FROM t")[0] is False


def test_explain_scan_plan_seqscan_sort():
    plan = ("Sort  (cost=1..2 rows=1)\n"
            "  ->  Seq Scan on t  (cost=0..1 rows=1)")
    kinds = {f.kind for f in explain.scan_plan(plan)}
    assert "seq_scan" in kinds and "sort" in kinds


def test_explain_is_dml():
    """is_dml 从 explain.py 挪到了 common.grmp.statement（三个 skill 曾各抄一份）。

    顺带钉住那条实测能写库的载荷：前导注释不能让 UPDATE 蒙混过关。
    """
    from common.grmp.statement import is_dml

    assert is_dml("DELETE FROM t")
    assert not is_dml("SELECT 1")
    assert is_dml("/* c */ UPDATE t SET a = 1")
    assert not explain.shape_reject("SELECT 1")
    assert "DML" in explain.shape_reject("/* c */ UPDATE t SET a = 1")


def test_slowsql_stmt_table_empty():
    out = slowsql.stmt_table("Slow SQL", [])
    assert "No matching statements" in out


def test_slowsql_stmt_table_rows():
    rows = [slowsql.StmtRow("123", "select 1", 5, 12.5, 0.06, 0.01, 5)]
    out = slowsql.stmt_table("Slow SQL", rows)
    assert "| SQL_ID |" in out
    assert "123" in out and "12.50" in out


def test_topsql_sort_keys_and_reject():
    # self / self_avg:2026-10-24 加的不含等待口径(CPU + 数据 IO),见 skills/gaussdb-topsql/scripts/waits.py
    assert topsql.SORT_KEYS == ["time", "avg", "calls", "reads", "rows", "self", "self_avg"]
    # Bad key must raise before touching the DB.
    try:
        topsql.top_sql(None, "bogus", 10)
    except ValueError as e:
        assert "must be one of" in str(e)
    else:
        raise AssertionError("expected ValueError for bad --by")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
