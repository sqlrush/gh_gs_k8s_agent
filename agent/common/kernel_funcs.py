"""GaussDB 私有内核函数的探测与使用:gs_get_explain(integer) / gs_get_kernel_info()。

背景(2026-09 现场四类报错之二):客户脚本调用这两个函数,目标实例上报 does not exist。
两个函数在 GaussDB 集中式与分布式文档里自 V2.0-3.x 起都有,openGauss 里**从来没有**
(og5 5.0.3 / og7 7.0.0 实测)。所以本模块的纪律是:

  · **先探测,后使用**:有就走内核函数(运行态计划、内核事务水位),没有就退回标准 EXPLAIN 等
    现有路径,两条路径的报告形状一致,只是注明来源;
  · **探测永不抛**:探测脚本本身跑不了(没注册、连不上)只算「未知」,由调用方决定退不退回;
  · **「该有而没有」只对 GaussDB 成立**:version() 含 GaussDB 而函数缺失才给中文说明
    (catalog 未升级 / 逐库不一致 / 实参类型),openGauss 缺失是常态,不报;
  · **签名按探测结果二选一,不按文档**:2026-09-08 客户在 505.2.1.SPC0600 的 pg_catalog 里查到的是
    gs_get_explain(bigint),文档写的 (integer) 已过时(pg_stat_activity.pid 本来就是 64 位线程号)。
    探到 bigint 走 explain.runtime_plan({{pid}}::bigint);探到 integer 走 explain.runtime_plan_int4
    且 pid 必须在 integer 范围内;两者都不是则不调、说明后退回 EXPLAIN。
    pid 参数位保持 INTEGER(中间件有数字校验,String 位是注入面),SQL 里再显式转型。

五条白名单脚本(走中间件必须先注册,见 docs/delivery/08-初始化白名单.md):
  explain.kernel_funcs       探测:version() + 两个函数在 pg_proc 里的签名
  explain.runtime_plan       SELECT gs_get_explain({{pid}}::bigint)   —— 505.2.1 实测签名
  explain.runtime_plan_int4  SELECT gs_get_explain({{pid}}::integer)  —— 文档签名的老内核
  explain.active_pid         按 unique_sql_id 找正在执行的会话(pid / query / query_start)
  explain.session_by_pid     按 pid 取该会话正在执行的 SQL
  vacuum.kernel_info         SELECT node_name, module, name, value FROM gs_get_kernel_info()
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .grmp.errors import QueryError

PROBE_SCRIPT = "explain.kernel_funcs"
RUNTIME_PLAN_SCRIPT = "explain.runtime_plan"            # gs_get_explain(bigint)——505.2.1 实测签名
RUNTIME_PLAN_INT4_SCRIPT = "explain.runtime_plan_int4"  # gs_get_explain(integer)——文档签名的老内核
ACTIVE_PID_SCRIPT = "explain.active_pid"
SESSION_BY_PID_SCRIPT = "explain.session_by_pid"
KERNEL_INFO_SCRIPT = "vacuum.kernel_info"

ENGINE_GAUSSDB = "gaussdb"
ENGINE_OPENGAUSS = "opengauss"
ENGINE_UNKNOWN = "unknown"

FUNC_EXPLAIN = "gs_get_explain"
FUNC_KERNEL_INFO = "gs_get_kernel_info"


class NoRuntimePlan(Exception):
    """函数在、调用也成功,但没有返回计划——文档写明的三个前提之一没满足。"""


@dataclass(frozen=True)
class KernelFuncs:
    engine: str
    version: str
    explain_args: Optional[str]      # None = pg_proc 里没有;否则是文档签名的实参列表(如 "integer")
    kernel_info: bool
    probe_error: str = ""            # 探测脚本本身失败的原因;非空时上面三项都是「未知」

    @property
    def has_explain(self) -> bool:
        return self.explain_args is not None

    @property
    def expected_but_missing(self) -> List[str]:
        """按文档该有、pg_proc 里却没有的函数——只对 GaussDB 判定。"""
        if self.engine != ENGINE_GAUSSDB or self.probe_error:
            return []
        out = []
        if not self.has_explain:
            out.append(FUNC_EXPLAIN)
        if not self.kernel_info:
            out.append(FUNC_KERNEL_INFO)
        return out


@dataclass(frozen=True)
class Session:
    pid: int
    query: str
    query_start: str = ""


def _engine_of(version: str) -> str:
    """先认 gaussdb 再认 opengauss:openGauss 的 version() 不含 "gaussdb" 这个子串
    (只有 "openGauss"),而商业版有的构建会在兼容说明里提到 openGauss。"""
    low = (version or "").lower()
    if "gaussdb" in low:
        return ENGINE_GAUSSDB
    if "opengauss" in low:
        return ENGINE_OPENGAUSS
    return ENGINE_UNKNOWN


def _first_value(row: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        if k in row and row[k] is not None:
            return str(row[k])
    return ""


def probe(runner) -> KernelFuncs:
    """一次往返:version() + 两个函数的签名。永不抛。"""
    try:
        rows = runner.run(PROBE_SCRIPT, {})
    except Exception as exc:                      # noqa: BLE001 —— 探测失败只是「未知」
        return KernelFuncs(ENGINE_UNKNOWN, "", None, False, probe_error=str(exc))
    version, explain_args, kernel_info = "", None, False
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = _first_value(row, "item")
        detail = _first_value(row, "detail")
        if item == "version":
            version = detail
        elif item == f"func:{FUNC_EXPLAIN}":
            explain_args = detail
        elif item == f"func:{FUNC_KERNEL_INFO}":
            kernel_info = True
    return KernelFuncs(_engine_of(version), version, explain_args, kernel_info)


def missing_note(kf: KernelFuncs) -> str:
    """给用户看的中文说明。openGauss 或探测正常且函数齐全 → 空串。"""
    if kf.probe_error:
        return (f"内核函数探测未完成(脚本 {PROBE_SCRIPT} 执行失败:{kf.probe_error})。"
                "本次按「不可用」处理,走现有路径;若该脚本尚未注册进白名单,请按交付文档补注册。")
    missing = kf.expected_but_missing
    if not missing:
        return ""
    names = " / ".join(missing)
    return (
        f"目标实例是 GaussDB({kf.version[:60]}),按官方文档(集中式与分布式自 V2.0-3.x 起)应包含 {names},"
        f"但当前连接的数据库 catalog 里查不到。可能原因:"
        f"① 集群升级后 catalog 升级未提交,函数尚未写入;"
        f"② pg_proc 逐 database 独立,脚本连接的这个库缺少该对象——请在**同一个 database** 里执行 "
        f"SELECT proname, pg_get_function_arguments(oid) FROM pg_proc WHERE proname IN ('{FUNC_EXPLAIN}','{FUNC_KERNEL_INFO}') "
        f"核对,并与 postgres 库比对;"
        f"③ 核对时连的库 / 实例与脚本实际执行的 dataIp 实例不是同一个(2026-09-08 客户在 grmp 库上查到三个函数齐全,"
        f"而报错发生在诊断任务打到的实例上),或该实例升级尚未提交(SHOW upgrade_mode;)。"
        f"另注意 505.2.1.SPC0600 实测签名是 gs_get_explain(bigint),文档写的 (integer) 已过时。"
        f"本次已退回现有路径(EXPLAIN 估算计划 / 标准视图)。"
    )


def _to_int(value: Any, what: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what} 必须是整数,收到 {value!r}") from None


INT4_MAX = 2_147_483_647
INT4_MIN = -2_147_483_648


def _arg_types(args: str) -> str:
    """pg_get_function_arguments 会带参数名与模式(如 "p integer"、"IN p integer"、"a text, b bigint");
    只留每个参数的类型,小写、逗号分隔,用来与文档签名比对。"""
    parts = []
    for raw in (args or "").split(","):
        tokens = raw.strip().split()
        if tokens:
            parts.append(tokens[-1].lower())
    return ",".join(parts)


def runtime_plan(runner, pid: Any, explain_args: str = "bigint") -> str:
    """gs_get_explain(pid) 的运行态计划文本。空结果按文档前提给出原因,不当成功。

    按探测到的签名二选一,前置检查都**不打数据库**,失败抛 NoRuntimePlan 让调用方退回 EXPLAIN:
      · (bigint) —— 505.2.1.SPC0600 现场实测的签名:走 explain.runtime_plan,任何 pid 都能传;
      · (integer) —— 官方文档写法:走 explain.runtime_plan_int4,但 pg_stat_activity.pid 是 64 位线程号
        (实测 281440978523808),超出 integer 范围时 ::integer 会报 integer out of range,超范围直接说明不调;
      · 其他签名 —— 两条脚本都对不上,不调。
    pid 以整数传给 INTEGER 参数位(中间件侧有数字校验;现场一直以 INTEGER 传超过 2^31 的 unique_sql_id 且正常),
    SQL 里再显式转型。
    """
    pid_int = _to_int(pid, "pid")
    sig = _arg_types(explain_args or "")
    if sig == "bigint":
        script = RUNTIME_PLAN_SCRIPT
    elif sig == "integer":
        if pid_int > INT4_MAX or pid_int < INT4_MIN:
            raise NoRuntimePlan(
                f"本内核的 gs_get_explain 签名是 (integer)(文档写法;505.2.1.SPC0600 实测已是 bigint),"
                f"而本实例 pg_stat_activity.pid 是 64 位线程号({pid_int}),超出 integer 范围,无法按该签名调用:"
                f"硬转 ::integer 会报 integer out of range,直接传 bigint 则报 function gs_get_explain(bigint) does not exist。"
                f"请确认内核版本与升级是否已提交,或改用 gs_get_dn_explain(node_name, global_sessionid);"
                f"分布式 CN 上的 pid 若在 integer 范围内则不受此限。")
        script = RUNTIME_PLAN_INT4_SCRIPT
    else:
        raise NoRuntimePlan(
            f"本内核的 gs_get_explain 签名是 ({explain_args}),已注册脚本只覆盖 (bigint)(505.2.1 实测)与 (integer)(文档),"
            f"未调用;请按实际签名补一条脚本。")
    rows = runner.run(script, {"pid": pid_int})
    lines = [str(next(iter(r.values()), "")) for r in (rows or []) if isinstance(r, dict)]
    text = "\n".join(ln for ln in lines if ln is not None).strip()
    if not text:
        raise NoRuntimePlan(
            f"gs_get_explain({pid_int}) 返回为空。按文档,需同时满足:track_activities=on;"
            f"plan_collect_thresh 不为 -1(为 -1 时始终返回空);目标语句可 EXPLAIN 且计划不含 STREAM 算子;"
            f"且该 pid 此刻确实在执行语句。分布式下它只看 CN 生成的计划,DN 上的需 gs_get_dn_explain。")
    return text


def _session_from(rows: Sequence[Dict[str, Any]]) -> Optional[Session]:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        pid = _first_value(row, "pid")
        if not pid:
            continue
        try:
            return Session(pid=int(pid), query=_first_value(row, "query"),
                           query_start=_first_value(row, "query_start"))
        except ValueError:
            continue
    return None


def active_session_for_sql(runner, sql_id: Any) -> Optional[Session]:
    """按 unique_sql_id 找一个正在执行该 SQL 的会话;没有就 None(不是错误)。"""
    rows = runner.run(ACTIVE_PID_SCRIPT, {"sql_id": _to_int(sql_id, "sql_id")})
    return _session_from(rows)


def session_by_pid(runner, pid: Any) -> Optional[Session]:
    rows = runner.run(SESSION_BY_PID_SCRIPT, {"pid": _to_int(pid, "pid")})
    return _session_from(rows)


def kernel_info(runner) -> List[Dict[str, Any]]:
    """gs_get_kernel_info() 的 node_name / module / name / value 行,原样返回,不解释。"""
    rows = runner.run(KERNEL_INFO_SCRIPT, {})
    return [r for r in (rows or []) if isinstance(r, dict)]


__all__ = ["KernelFuncs", "Session", "NoRuntimePlan", "probe", "missing_note", "runtime_plan",
           "active_session_for_sql", "session_by_pid", "kernel_info", "QueryError",
           "PROBE_SCRIPT", "RUNTIME_PLAN_SCRIPT", "RUNTIME_PLAN_INT4_SCRIPT", "ACTIVE_PID_SCRIPT",
           "SESSION_BY_PID_SCRIPT", "KERNEL_INFO_SCRIPT"]
