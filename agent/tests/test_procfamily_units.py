"""DB-free unit tests for the topproc / procinfo skills."""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _dir(skill: str) -> str:
    """技能目录一律带 gaussdb- 前缀；调用方两种写法都收。

    这里曾经写死 skills/<skill>/，目录名却是 skills/gaussdb-<skill>/ ——
    收集阶段就 FileNotFoundError，整个 tests/ 一起中断。也就是说本文件
    自行方版导入起从未跑过，看着像覆盖，实际一条没执行。
    """
    return skill if skill.startswith("gaussdb-") else "gaussdb-" + skill


def _load(skill: str, mod: str):
    path = _ROOT / "skills" / _dir(skill) / "scripts" / f"{mod}.py"
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(f"{skill}_{mod}", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


topproc = _load("topproc", "topproc")
procinfo = _load("procinfo", "procinfo")


def test_procinfo_resolves_package_procs_and_refuses_ambiguity():
    """三段名 schema.package.proc 能定位到包内过程;同名过程不止一个又没指定包时拒绝,不猜。"""
    class _R:
        def __init__(self):
            self.calls = []

        def run(self, script, values=None):
            v = dict(values or {})
            self.calls.append(v)
            rows = [{"nspname": "gmag", "proname": "proc_x", "lanname": "plpgsql", "prosrc": "BEGIN NULL; END",
                     "args": "", "package": p} for p in ("pkg_a", "pkg_b")]
            return [r for r in rows if r["proname"] == v["name"]
                    and (not v["schema"] or r["nspname"] == v["schema"])
                    and (not v["package"] or r["package"] == v["package"])]

    import pytest
    d = procinfo.fetch_proc_def(_R(), "gmag.pkg_a.proc_x")
    assert d.schema == "gmag" and d.package == "pkg_a" and d.name == "proc_x"
    with pytest.raises(ValueError) as ei:
        procinfo.fetch_proc_def(_R(), "gmag.proc_x")
    assert "gmag.pkg_a.proc_x" in str(ei.value) and "gmag.pkg_b.proc_x" in str(ei.value)


def test_procinfo_structural_scan_detects_antipatterns():
    # Reuse the vendored procanalyze engine to confirm the four kinds fire.
    import procanalyze as pa
    body = (
        "BEGIN\n"
        "  FOR r IN SELECT id FROM t LOOP\n"
        "    UPDATE t SET x = 1 WHERE id = r.id;\n"
        "    EXECUTE 'select 1';\n"
        "    BEGIN\n"
        "      PERFORM 1;\n"
        "    EXCEPTION WHEN OTHERS THEN NULL;\n"
        "    END;\n"
        "  END LOOP;\n"
        "END;\n"
    )
    kinds = {f.kind for f in pa.scan_structure(body)}
    assert "per_row_dml" in kinds
    assert "dynamic_sql" in kinds
    assert "exception_in_loop" in kinds


def test_procinfo_report_sections():
    import procanalyze as pa
    proc = pa.analyze("public", "p", "plpgsql",
                      "BEGIN\n  UPDATE t SET x=1;\nEND;", "a integer")
    pe = procinfo.ProcEvidence(
        proc=proc, structure=pa.scan_structure(proc.body),
        embedded=[], runtime_note="rt-note",
        gucs=[procinfo.GUC("work_mem", "4096", "kB")])
    out = procinfo.proc_info_report(pe)
    for section in ("## Procedure Source", "## Structural Findings",
                    "## Embedded Statements", "## Runtime Attribution",
                    "## Key Parameters (GUC)"):
        assert section in out
    assert "public.p" in out and "work_mem" in out


def test_topproc_sort_keys_and_reject():
    assert topproc.SORT_KEYS == ["time", "self", "calls"]
    try:
        topproc.top_procs(None, "bogus", 10)
    except ValueError as e:
        assert "must be one of" in str(e)
    else:
        raise AssertionError("expected ValueError for bad --by")


def test_topproc_table_empty():
    out = topproc.proc_table("time", [], topproc._EMPTY_NOTE)
    assert "无函数级统计" in out
    assert "track_functions" in out


def test_topproc_table_rows():
    rows = [topproc.ProcStat("public", "f1", 7, 123.45, 100.0)]
    out = topproc.proc_table("time", rows, "")
    assert "| PROCEDURE |" in out
    assert "public.f1" in out and "123.45" in out
    assert "gaussdb-procinfo" in out and "gaussdb-proctune" in out


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


# ---- topproc 统计为空时查清原因(2026-11-11 现场:没切到业务库却被说成 track_functions=none) ----

class _Client:
    def __init__(self, target_db, routes):
        self.target_db, self._routes = target_db, routes

    def routes(self, name):
        return self._routes


class _Runner:
    def __init__(self, target_db="", routes=False, tf="none", settings_fail=False):
        self.client = _Client(target_db, routes)
        self.tf, self.settings_fail, self.calls = tf, settings_fail, []

    def run(self, script, values=None):
        self.calls.append(script)
        if script == "topproc.top_procs":
            return []
        if self.settings_fail:
            raise RuntimeError("boom")
        return [{"name": "shared_buffers", "setting": "1"}, {"name": "track_functions", "setting": self.tf}]


def test_empty_on_postgres_says_not_switched_and_does_not_blame_the_parameter():
    _, note = topproc.top_procs(_Runner(target_db="", tf="pl"), "time", 10)
    assert "在实例的 postgres 库上查的" in note and "会话没有切到业务库" in note
    assert "不要据此说 track_functions 关着" in note and "track_functions=pl" in note


def test_empty_when_the_bank_whitelist_lacks_db_name_names_the_delta():
    _, note = topproc.top_procs(_Runner(target_db="appdb", routes=False), "time", 10)
    assert "scripts-delta-20261111.sql" in note and "postgres 库上查的" in note


def test_empty_on_the_business_db_with_parameter_off_says_so():
    _, note = topproc.top_procs(_Runner(target_db="appdb", routes=True, tf="none"), "time", 10)
    assert "已在「appdb」上查" in note and "track_functions=none" in note and "设为 pl" in note


def test_empty_on_the_business_db_with_parameter_on_says_no_calls_yet():
    _, note = topproc.top_procs(_Runner(target_db="appdb", routes=True, tf="pl"), "time", 10)
    assert "已开启,但还没有调用记录" in note and "长连接" in note


def test_empty_with_settings_unreadable_asks_the_dba_to_check():
    _, note = topproc.top_procs(_Runner(target_db="appdb", routes=True, settings_fail=True), "time", 10)
    assert "现值没取到" in note and "SHOW track_functions" in note


def test_non_empty_result_does_not_read_settings():
    class R(_Runner):
        def run(self, script, values=None):
            self.calls.append(script)
            return [{"nspname": "s", "proname": "p", "calls": "1", "total_ms": "2", "self_ms": "1"}]
    r = R(target_db="appdb", routes=True)
    rows, note = topproc.top_procs(r, "time", 10)
    assert len(rows) == 1 and note == "" and r.calls == ["topproc.top_procs"]
