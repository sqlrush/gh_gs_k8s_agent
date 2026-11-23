"""行方白名单核对小工具(agent/tools/compare_whitelist.py,2026-11-12)。

行方 11-12 的分析报告把「增量没导入」当成了代码问题;这份工具拿白名单团队的导出逐条比对,直接说出缺哪些、
哪些不一致、该按什么顺序导哪几份增量。口径复用发版台的白名单关卡(release/whitelist.py)。
"""
import csv
import importlib.util
import io
import json
import pathlib
import sys

_AGENT = pathlib.Path(__file__).resolve().parents[1]
_ROOT = _AGENT.parent
sys.path.insert(0, str(_ROOT))
_spec = importlib.util.spec_from_file_location("compare_whitelist_under_test", _AGENT / "tools" / "compare_whitelist.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)
from release import whitelist as wl  # noqa: E402

DELIVERY = _ROOT / "docs" / "delivery"
FULL = (DELIVERY / "scripts.sql").read_text(encoding="utf-8")
EXPECTED = wl.parse_dml(FULL)
TYPES = cw.dbtypes_from_dml(FULL)


def _rows():
    return {n: {"script_name": n, "content_md5": s.md5,
                "parameter_config": json.dumps([{"key": k, "value": "", "type": t, "autoAcquire": False} for k, t in s.params]),
                "is_valid": "1", "database_type": TYPES.get(n, "postgres")} for n, s in EXPECTED.items()}


def _csv(rows, with_type=True):
    buf = io.StringIO()
    cols = ["script_name", "content_md5", "parameter_config", "is_valid"] + (["database_type"] if with_type else [])
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows.values():
        w.writerow(r)
    return buf.getvalue()


def _run(text):
    out = io.StringIO()
    return cw.run(text, DELIVERY, out=out), out.getvalue()


def test_a_matching_export_needs_nothing():
    rc, out = _run(_csv(_rows()))
    assert rc == 0 and "不用导入" in out and "已登记且一致 %d 条" % len(EXPECTED) in out


def test_the_bank_state_names_what_differs_and_the_deltas_in_order():
    rows = _rows()
    rows.pop("memanalyze.sql_mem_history")                       # 20261112 新增
    rows["health.storage"]["content_md5"] = "0" * 32             # 20261108 改过正文
    rows["explain.from_history"]["database_type"] = "appbusiness"  # 20261111 改过执行库
    rows["explain.multi_stmt_probe"] = dict(rows["health.storage"], script_name="explain.multi_stmt_probe")
    rc, out = _run(_csv(rows))
    assert rc == 1
    assert "memanalyze.sql_mem_history:未登记" in out and "health.storage:正文不一致" in out
    assert "explain.from_history:执行库不一致 —— 应为 postgres,实为 appbusiness" in out
    assert "本版不再使用" in out and "explain.multi_stmt_probe" in out
    order = [f for f in ("scripts-delta-20261108.sql", "scripts-delta-20261111.sql", "scripts-delta-20261112.sql") if f in out]
    assert order == ["scripts-delta-20261108.sql", "scripts-delta-20261111.sql", "scripts-delta-20261112.sql"]
    assert out.index("20261108") < out.index("20261111") < out.index("20261112")


def test_an_export_without_database_type_still_compares_content_and_params():
    rows = _rows()
    rows["health.storage"]["content_md5"] = "0" * 32
    rc, out = _run(_csv(rows, with_type=False))
    assert rc == 1 and "执行库没有比对" in out and "health.storage:正文不一致" in out


def test_a_wrong_header_shows_the_export_sql():
    rc, out = _run("name,md5\na.b,1\n")
    assert rc == 2 and "SELECT script_name, md5(script_content)" in out
