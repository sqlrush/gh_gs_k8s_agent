"""Live tests for sqlreview's catalog collection (auto-skip without a DB).

Unit tests mock the DB, so they can never catch dialect SQL errors. This file
exists because they didn't: the index query used `WITH ORDINALITY` (PostgreSQL
9.4+), which openGauss — based on 9.2 — rejects outright. The collector degraded
exactly as designed and reported the reason, but the index layer went blind and
every index rule silently stopped firing. Only a real server catches that.

Run with:  pytest -m live
"""
import importlib.util
import os
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-sqlreview" / "scripts"

CONN = os.environ.get("SQLREVIEW_LIVE_CONN", "og")


# Several skills ship a module named `model` / `render`. Whichever test file ran
# first leaves its own copy in sys.modules, and sqlreview's `from model import ...`
# would then resolve against another skill's model. Drop them before loading ours
# (same guard as tests/test_health_units.py).
_SIBLINGS = ("model", "lexer", "rules", "checks", "objects", "report", "render")


def _load(mod: str):
    for name in _SIBLINGS:
        sys.modules.pop(name, None)
    path = _SCRIPTS / f"{mod}.py"
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[mod] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def runner():
    """统一入口的 runner，不是原始连接：走中间件还是直连由 CONN 的 driver 决定。

    把 SQLREVIEW_LIVE_CONN 指到一条 driver: grmp 的连接，同一批用例就
    变成中间件路径的验收 —— 这正是统一入口存在的意义。

    跳过的判据只有「连接没配」一条（与 test_dual_path_live.py 一致）。
    连上了但脚本跑不通要**失败**，不能跳过 —— 这个文件存在的理由就是
    catch 那种「降级成功、索引层其实全瞎了」的情况。
    """
    sys.path.insert(0, str(_ROOT))
    import common
    from common import access
    try:
        common.find(CONN)
    except common.ConfigError as exc:
        pytest.skip(f"connection {CONN!r} not configured: {exc}")
    return access.for_conn(CONN)


@pytest.mark.live
def test_catalog_queries_parse_on_a_real_server(runner):
    """Every object query must actually run — a degraded layer is a blind layer."""
    objects = _load("objects")
    facts = objects.collect_facts(runner, "pg_catalog")  # always exists, always populated
    assert facts.notes == (), f"catalog collection degraded: {facts.notes}"
    assert facts.tables, "no tables collected from pg_catalog"
    assert facts.indexes, "no indexes collected — the index query silently went blind"


@pytest.mark.live
def test_index_columns_are_resolved_in_order(runner):
    """indkey -> column names must survive the openGauss dialect, in order."""
    objects = _load("objects")
    facts = objects.collect_facts(runner, "pg_catalog")
    multi = [i for i in facts.indexes if len(i.columns) > 1]
    assert multi, "no multi-column index resolved — column extraction is broken"
    for idx in facts.indexes:
        assert all(c and not c.isdigit() for c in idx.columns), \
            f"{idx.name}: columns look like raw attnums, not names: {idx.columns}"
