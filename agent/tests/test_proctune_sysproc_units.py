"""proctune:系统自带的存储过程一律不调优(用户 2026-09-21 定)。

sqltune 早就有系统对象闸(skills/gaussdb-sqltune/scripts/systables.py),proctune 一直没有。
用户要求两个 skill 一致:只调优用户自己的 SQL 与存储过程,高斯自带的全部忽略。

与 sqltune 对齐的三条:
① 判定按 schema 名走 common/sysobjects.py 那份共用名单(proctune 拿到的报文里没有 oid);
② 跳过是**确定性结论,不是失败** —— exit 0,免得现场 agent 当错误反复重试;
③ 报告要说清为什么跳过、跳过的是什么,不留「什么都没发生」的空白。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-proctune" / "scripts"))

import sysprocs  # noqa: E402


@pytest.mark.parametrize("schema", ["pg_catalog", "dbe_perf", "snapshot", "pkg_service",
                                    "dbe_sql_util", "xmltype", "coverage", "sys"])
def test_system_schema_procs_are_skipped(schema):
    assert sysprocs.verdict(schema, "some_proc").is_system


@pytest.mark.parametrize("schema", ["public", "cbst", "gmag", "app1", "gaussdb"])
def test_user_procs_are_analyzed(schema):
    """public 也是用户的 —— 它 oid 虽小,却是用户建对象的地方。"""
    assert not sysprocs.verdict(schema, "p_settle").is_system


def test_verdict_carries_the_qualified_name_for_the_report():
    v = sysprocs.verdict("dbe_perf", "get_global_full_sql_by_timestamp")
    assert v.qualified == "dbe_perf.get_global_full_sql_by_timestamp"


def test_package_proc_keeps_all_three_parts():
    """包内过程是 schema.package.proc,报告里要能看出是哪个包。"""
    v = sysprocs.verdict("pkg_service", "job_submit", package="dbms_job")
    assert v.is_system and v.qualified == "pkg_service.dbms_job.job_submit"


def test_skip_report_says_what_and_why():
    r = sysprocs.skip_report(sysprocs.verdict("dbe_perf", "get_summary_statement"))
    assert "dbe_perf.get_summary_statement" in r
    assert "不做调优" in r
    # 必须说明是确定性结论,否则现场 agent 会反复重试
    assert "重试" in r


def test_skip_json_shape_matches_sqltune():
    """两个 skill 的跳过输出形状要一致,下游(health 汇总 / 模型)才不用分别处理。"""
    j = sysprocs.skip_json(sysprocs.verdict("snapshot", "snap_stat"))
    assert j["skipped"] is True
    assert j["reason"] == "system-proc"
    assert j["proc"] == "snapshot.snap_stat"


def test_unknown_schema_is_treated_as_user():
    """取不到 schema 时放行 —— 保守方向是多做一份无害的分析,不是吞掉用户的请求。"""
    assert not sysprocs.verdict("", "p").is_system
    assert not sysprocs.verdict(None, "p").is_system
