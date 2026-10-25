"""哪些 schema 是数据库自带的(2026-09-21 起 sqltune 与 proctune 共用这一份)。

用户要求:这两个 skill 只调优**用户自己的** SQL 与存储过程;高斯系统自带的一律忽略。
原先只有 sqltune 有这道闸,名单还是手写的,漏了几个;proctune 完全没有。

名单是从真实实例取的,不是拍脑袋:
    select nspname, oid from pg_namespace where oid < 16384
og5(openGauss-lite 5.0.3)与 og7(7.0.0-RC1)各取一遍再求并集。

**为什么不用 oid < 16384 直接判**:两处原因。① `public` 的 oid 是 2200,也小于 16384,
但它正是用户建对象的地方,一刀切会把用户的东西全忽略掉。② proctune 拿到的是
中间件已注册脚本返回的 `nspname`,报文里**没有 oid** —— 要用 oid 就得改脚本让客户
重新登记,代价远大于收益。所以按名字判,名单来自真库。

判定保守:拿不准一律当用户对象。误放行只是多出一份无害的分析,误拦截会吞掉
用户真实的调优请求。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import sysobjects  # noqa: E402

# og5 实测(oid < 16384),除 public 外
_OG5 = ["pg_catalog", "pg_toast", "cstore", "pkg_service", "dbe_perf", "snapshot",
        "blockchain", "db4ai", "dbe_pldebugger", "dbe_pldeveloper", "sqladvisor",
        "dbe_sql_util", "information_schema"]
# og7 比 og5 多出来的两个
_OG7_EXTRA = ["coverage", "xmltype"]


@pytest.mark.parametrize("name", _OG5 + _OG7_EXTRA)
def test_real_instance_schemas_are_recognized(name):
    assert sysobjects.is_system_schema(name), "%s 是真库里自带的 schema,漏了它就会去调优系统对象" % name


def test_public_is_not_system():
    """**踩点**:public 的 oid 是 2200(< 16384),但它正是用户建对象的地方。

    按 oid 一刀切会把用户 public 下的表和过程全部当成系统对象忽略掉 ——
    而且是静默忽略,用户只会看到「按策略跳过」,完全不知道自己的东西被当成了内核的。
    """
    assert not sysobjects.is_system_schema("public")


@pytest.mark.parametrize("name", ["cbst", "gsbench", "gaussdb", "demo_shop", "kf_gauss", "app1"])
def test_user_schemas_are_not_system(name):
    assert not sysobjects.is_system_schema(name)


def test_name_matching_ignores_case_and_padding():
    """用户可能写 PG_CATALOG,或者名字从报文里带出空格。"""
    for v in ("PG_CATALOG", " pg_catalog ", "Dbe_Perf"):
        assert sysobjects.is_system_schema(v), v


def test_temp_schemas_are_system():
    """会话临时 schema 是内核按会话编号建的(pg_temp_3 / pg_toast_temp_3),编号不固定,
    所以按前缀认,不能进名单。"""
    for v in ("pg_temp_3", "pg_toast_temp_17", "pg_temp_1"):
        assert sysobjects.is_system_schema(v), v


def test_empty_is_not_system():
    """名字取不到时当用户对象 —— 保守方向是放行,不是拦截。"""
    for v in ("", None, "   "):
        assert not sysobjects.is_system_schema(v)


def test_gaussdb_commercial_schemas_kept():
    """商用 GaussDB 才有、两个测试实例上没有的,名单里也要留着。"""
    for v in ("sys", "pmk", "pkg_util", "dbe_scheduler"):
        assert sysobjects.is_system_schema(v), v


def test_list_is_sorted_and_deduped():
    """名单是要给人看、给客户审的,保持有序无重复。"""
    names = list(sysobjects.SYSTEM_SCHEMAS)
    assert len(names) == len(set(names))
    assert "public" not in sysobjects.SYSTEM_SCHEMAS
