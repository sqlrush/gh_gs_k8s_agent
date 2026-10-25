"""注册脚本的形态检查 —— 白名单模式下这两条是 lockwait 的全部取数来源。"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_REG = _ROOT / "scripts" / "registry"

from common.grmp.script import load_script  # noqa: E402


@pytest.mark.parametrize("rel,name", [
    ("lockwait/pairs.yaml", "lockwait.pairs"),
    ("lockwait/chain.yaml", "lockwait.chain"),
])
def test_script_loads_and_is_readonly(rel, name):
    rec = load_script(_REG / rel)
    assert rec.script_name == name
    assert rec.readonly is True, "%s 不是只读 —— 诊断脚本不该能写" % rel


def test_pairs_returns_every_column_the_report_needs():
    """列名是**契约**。少一列，报告里那一栏会静默变空。全部 18 列都要覆盖 ——
    覆盖不全的守卫等于没守卫：漏掉的那几列照样能悄悄消失而没有测试报警。"""
    sql = load_script(_REG / "lockwait/pairs.yaml").script_content
    for col in ("waiter_pid", "waiter_sessionid", "waiter_mode",
                "holder_pid", "holder_sessionid", "holder_mode",
                "locktype", "lock_object", "locktag",
                "waiter_wait_s", "waiter_user", "waiter_app", "waiter_query",
                "holder_state", "holder_user", "holder_app",
                "holder_xact_age_s", "holder_query"):
        assert col in sql, "pairs.yaml 少了列 %s" % col


def test_pairs_joins_holder_and_waiter_on_the_same_locktag():
    sql = load_script(_REG / "lockwait/pairs.yaml").script_content
    assert "granted" in sql, "要靠 granted 区分持有者与等待者"
    assert "locktag" in sql


def test_chain_gives_the_edge_not_an_aggregate():
    """链要的是**边**（谁等谁），根由 python 侧上溯算 —— 聚合过的链没法找根。"""
    sql = load_script(_REG / "lockwait/chain.yaml").script_content
    assert "block_sessionid" in sql
    assert "count(" not in sql.lower(), "chain 不该在 SQL 里聚合"
