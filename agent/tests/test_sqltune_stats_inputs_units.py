"""代价推演所需统计输入的采集层单测（不连库）。

这几列是推演的输入，它们的坏法是同一种：取回来了、类型也对、只是值错了
或者键名对不上，而报告照常生成 —— 推演会拿着错数算出一个精确的错结论。
所以在采集这一层钉死，不留到连库才发现。

模块按路径显式加载：proctune 下有一个同名的 evidence.py，`import evidence`
取到哪一个取决于测试文件的收集顺序。让它依赖顺序，早晚会出现「本地绿、
换个 -k 参数就红」。
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "skills" / "gaussdb-sqltune" / "scripts"
sys.path.insert(0, str(_SCRIPTS))   # evidence.py 要 import 同目录的 render
sys.path.insert(0, str(_ROOT))      # common

from common.grmp.script import load_script  # noqa: E402


def _load_evidence():
    spec = importlib.util.spec_from_file_location(
        "sqltune_evidence_for_stats_test", _SCRIPTS / "evidence.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


evidence = _load_evidence()


class _Runner:
    """按脚本逻辑名喂固定行。值一律是字符串 —— 协议就是这么给的。"""

    def __init__(self, rows_by_script):
        self._rows = rows_by_script
        self.calls = []

    def run(self, script, params=None):
        self.calls.append((script, params))
        return self._rows.get(script, [])


_INDEX_ROW = {
    "table_name": "customers",
    "index_name": "customers_pkey",
    "indisunique": "t",
    "indisprimary": "t",
    # openGauss 的 pg_class.relpages 是 double precision，经协议就是这个形态
    "index_relpages": "274.0",
    "index_reltuples": "100000000",
    "index_def": "CREATE UNIQUE INDEX customers_pkey ON customers USING btree (id)",
}

_FRESHNESS_ROW = {
    "schemaname": "gsbench",
    "relname": "customers",
    "n_live_tup": "100000000",
    "n_dead_tup": "0",
    "last_analyze": "2026-08-01 03:12:44",
    "last_autoanalyze": "never",
    "analyze_count": "1",
    "autoanalyze_count": "0",
}


# --- 索引规模列 --------------------------------------------------------------

def test_collect_indexes_reads_index_size_columns():
    """索引页数取自 pg_class 里**索引那一行**，且接受 double 形态。

    "274.0" 直接 int() 会抛 ValueError；换成表那一行的 relpages 则会得到
    一个大几个数量级的数，算出来的树高小得离谱 —— 而两种坏法的输出
    都还是「一个数字」。
    """
    runner = _Runner({evidence.INDEXES_SCRIPT: [_INDEX_ROW]})
    ix = evidence.collect_indexes(runner, ["customers"])[0]
    assert ix.pages == 274
    assert ix.tuples == 100000000


def test_collect_indexes_fields_do_not_shift():
    """加了两个位置参数后，后面的字段没有整体错位。

    IndexInfo 是位置构造的，中间插字段最典型的坏法是 definition 变成条目数
    ——不报错，报告里的 DEF 列变成一串数字，而没人会盯着那一列看。
    """
    runner = _Runner({evidence.INDEXES_SCRIPT: [_INDEX_ROW]})
    ix = evidence.collect_indexes(runner, ["customers"])[0]
    assert ix.table == "customers"
    assert ix.name == "customers_pkey"
    assert ix.is_unique is True
    assert ix.is_primary is True
    assert ix.definition.startswith("CREATE UNIQUE INDEX")


def test_collect_indexes_bool_columns_survive_f():
    """bool("f") 是 True —— 普通索引不能被报成 UNIQUE。"""
    row = dict(_INDEX_ROW, indisunique="f", indisprimary="f")
    runner = _Runner({evidence.INDEXES_SCRIPT: [row]})
    ix = evidence.collect_indexes(runner, ["customers"])[0]
    assert ix.is_unique is False
    assert ix.is_primary is False


# --- 统计新鲜度 --------------------------------------------------------------

def test_collect_stats_freshness_maps_columns():
    runner = _Runner({evidence.STATS_FRESHNESS_SCRIPT: [_FRESHNESS_ROW]})
    fr = evidence.collect_stats_freshness(runner, ["customers"])[0]
    assert fr.schema == "gsbench"
    assert fr.table == "customers"
    assert fr.live_tuples == 100000000
    assert fr.dead_tuples == 0
    assert fr.last_analyze == "2026-08-01 03:12:44"
    assert fr.analyze_count == 1


def test_collect_stats_freshness_keeps_never_distinct_from_blank():
    """'never'（从未 ANALYZE）必须原样留住。

    脚本里已经 COALESCE 成 'never'，所以取回空串意味着协议层出了问题，
    两者不能在取值代码里被抹平成同一个东西 —— 推演层要靠这个区分
    「这表从没分析过」和「这一列没取到」，两者都该拒绝出数，但原因要说得对。
    """
    row = dict(_FRESHNESS_ROW, last_analyze="never", last_autoanalyze="")
    runner = _Runner({evidence.STATS_FRESHNESS_SCRIPT: [row]})
    fr = evidence.collect_stats_freshness(runner, ["customers"])[0]
    assert fr.last_analyze == "never"
    assert fr.last_autoanalyze == ""


def test_collect_stats_freshness_short_circuits_on_empty_names():
    """空表名列表不发请求 —— IN () 是语法错误。"""
    runner = _Runner({})
    assert evidence.collect_stats_freshness(runner, []) == []
    assert runner.calls == []


# --- yaml 别名与代码读的键必须一致 -------------------------------------------

_EXPECTED_KEYS = {
    "sqltune/indexes.yaml": [
        "table_name", "index_name", "indisunique", "indisprimary",
        "index_relpages", "index_reltuples", "index_def",
    ],
    "sqltune/stats_freshness.yaml": [
        "schemaname", "relname", "n_live_tup", "n_dead_tup",
        "last_analyze", "last_autoanalyze", "analyze_count", "autoanalyze_count",
    ],
    "sqltune/key_gucs.yaml": ["cpu_tuple_cost", "cpu_index_tuple_cost",
                              "cpu_operator_cost", "block_size", "query_dop"],
    "sqltune/tables.yaml": ["relpages", "reltuples", "curpages"],
}


@pytest.mark.parametrize("rel,keys", sorted(_EXPECTED_KEYS.items()))
def test_registry_exposes_the_keys_the_code_reads(rel, keys):
    """脚本里改了别名、代码没跟着改 —— 只有连上真库才会 KeyError。

    单测再多也覆盖不到，因为假 runner 喂的是代码自己期望的键名。
    所以这里拿注册脚本的正文对一遍。
    """
    sql = load_script(_ROOT / "scripts" / "registry" / rel).script_content
    for key in keys:
        assert key in sql, (
            "%s 的正文里找不到 %r —— 取值代码按这个键读，"
            "对不上时本地全绿、连库当场 KeyError。" % (rel, key))
