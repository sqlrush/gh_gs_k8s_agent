"""把数据库 / 中间件的原始报错翻成一句现场 DBA 看得懂的话。

只做「已知模式 → 提示」的**追加**:原文一字不动、不吞,提示跟在后面。
原因:现场把「HTTP Error 400:」后面一片空白追成了参数名问题,两天没碰到真因
(备机读不了 unlogged 表)。原文是证据,提示是方向,两样都要在。

两条访问路径共用:中间件路径(client.py)与直连路径(runner.py)。
"""
from __future__ import annotations

import re
from typing import Sequence, Tuple

# (任一子串命中(不分大小写) → 提示)。顺序即优先级,只取第一条命中的。
_HINTS: Sequence[Tuple[Tuple[str, ...], str]] = (
    (("cannot be accessed on the standby",),
     "当前连接的是备机:dbe_perf.statement_history 这类 unlogged 表在备机上读不到。"
     "用主库 IP 重新 gaussdb-login;或接受降级——取 SQL 文本时会退到 dbe_perf.statement"
     "(归一化文本,参数值是占位符,可用 --bind 补)"),
    # 备机的第二种形态(2026-09-06 现场):恢复期禁用 WAL 控制函数,如 pg_current_xlog_location()
    (("recovery is in progress", "cannot be executed during recovery"),
     "当前连接的是备机(实例处于恢复态):WAL 控制函数(如 pg_current_xlog_location)在备机上被禁止执行。"
     "用主库 IP 重新 gaussdb-login;若是调度侧把诊断任务派到了备机,请在调度侧核对 dataIp 的主备属性"),
    (("enable_stmt_track", "track_stmt_stat_level", "track_stmt_parameter"),
     "实例未开启语句跟踪(enable_stmt_track / track_stmt_stat_level),statement 类视图没有数据;请 DBA 开启后再查"),
    (("permission denied",), ""),                     # 占位:文案由 _permission_hint 按对象名生成
    (("canceling statement due to statement timeout", "statement timeout", "query timeout"),
     "语句超时被取消:缩小时间窗或提高阈值让结果变少;中间件侧的超时由 GRMP 配置决定"),
    (("查询不到对应高斯实例", "instance not found"),
     "中间件按 dataIp 找不到实例:登录用的 IP 必须是 GRMP 里登记的实例 IP,不是主机管理 IP"),
    # 函数级 does not exist 必须排在通用 does not exist 之前:三种原因不同,给的动作也不同
    (("function ", " does not exist"), ""),             # 占位:文案由 _function_hint 生成(需两个子串同时命中)
    (("relation ", " does not exist"), ""),             # 占位:文案由 _relation_hint 生成(不带 schema 的表先怀疑 search_path)
    (("does not exist",),
     "对象不存在:多半是版本差异(视图 / 列名不同)或脚本注册到了别的库——对照 whitelist.md 里的 SQL 与目标实例版本"),
)

_RELATION_RE = re.compile(r"permission denied for (relation|table|view|function|schema|sequence)\s+([\w.\"]+)", re.I)
_FUNCTION_RE = re.compile(r"function\s+([\w.\"]+)\s*\(([^)]*)\)\s+does not exist", re.I)


def _permission_hint(text: str) -> str:
    """按报错点名的对象种类给动作:schema 要 USAGE(监控 schema 实为 MONADMIN),函数要 EXECUTE,表/视图要 SELECT。
    一律说「授 SELECT」会让 DBA 对 dbe_perf 白授一次——现场实测 kf_lowpriv 读 dbe_perf 报的正是 schema 级。"""
    m = _RELATION_RE.search(text or "")
    if not m:
        return ("执行账号没有该对象的访问权限:请 DBA 按报错点名的对象授权"
                "(系统表如 pg_user_status 默认只对高权限角色开放;也可改用有权限的视图)")
    kind, obj = m.group(1).lower(), m.group(2).strip('"')
    if kind == "schema":
        return (f"执行账号没有 schema {obj} 的访问权限:请 DBA 给执行账号授予该 schema 的 USAGE 权限;"
                f"dbe_perf / snapshot 这类监控 schema 实际要 MONADMIN(监控管理员)角色,GRANT 单个对象不够")
    if kind == "function":
        return (f"执行账号没有函数 {obj} 的执行权限:请 DBA 给执行账号授予 EXECUTE 权限"
                f"(内核私有函数通常只对 sysadmin / monadmin 开放)")
    return (f"执行账号没有对象 {obj} 的访问权限:请 DBA 给执行账号授予 {obj} 的 SELECT 权限"
            f"(系统表如 pg_user_status 默认只对高权限角色开放;也可改用有权限的视图)")


def _function_hint(text: str) -> str:
    m = _FUNCTION_RE.search(text or "")
    if not m:
        return ""
    name, args = m.group(1).strip('"'), (m.group(2) or "").strip()
    return (
        f"函数 {name} 按「名字 + 实参类型({args or '无参'})」在当前 database 的 pg_proc 里找不到匹配。"
        f"报错里的实参类型是**调用时传的**,不代表已存在的重载。三种原因分开查:"
        f"① 函数存在但实参类型不符(int8→int4 无隐式转换;注意 505.2.1.SPC0600 实测签名是 gs_get_explain(bigint),"
        f"文档写的 (integer) 已过时,pid 是 64 位线程号应按 bigint 传)——按 pg_proc 里的真实签名显式转型;"
        f"② pg_proc 逐 database 独立——在脚本连接的同一个 database 里执行 "
        f"SELECT proname, pg_get_function_arguments(oid) FROM pg_proc WHERE proname = '{name}',并与 postgres 库比对;"
        f"③ 集群升级后 catalog 升级未提交,新函数尚未写入。openGauss 本身没有 gs_get_explain / gs_get_kernel_info,"
        f"对 openGauss 这不算异常"
    )


_RELATION_MISSING_RE = re.compile(r'relation\s+"?([\w.]+)"?\s+does not exist', re.I)


def _relation_hint(text: str) -> str:
    """表 / 视图不存在。不带 schema 的名字先怀疑 search_path(sqltune 现场 400 的成因):业务 SQL 的表名
    靠应用账号的 search_path 解析,中间件执行账号的 search_path 是 "$user", public,解析不到。
    带 schema 的多半是系统视图,那才是版本差异 / 注册错库。"""
    m = _RELATION_MISSING_RE.search(text or "")
    if not m:
        return ""
    name = m.group(1)
    if "." in name:
        return (f"对象 {name} 不存在:多半是版本差异(视图 / 列名不同)或脚本注册到了别的库——"
                f"对照 whitelist.md 里的 SQL 与目标实例版本")
    return (f"表 / 视图 {name} 找不到,而它没带 schema:多半不是不存在,是执行账号的 search_path 里没有它所在的 schema。"
            f"业务 SQL 的表名靠应用账号的 search_path 解析,中间件执行账号的 search_path 默认是 \"$user\", public。"
            f"核对:SELECT current_schemas(true); 与 SELECT nspname FROM pg_namespace n JOIN pg_class c ON c.relnamespace = n.oid "
            f"WHERE c.relname = '{name}';。解决:由 DBA 给执行账号在该库上设置 search_path 包含业务 schema"
            f"(ALTER ROLE <执行账号> IN DATABASE <库> SET search_path = <业务schema>, public),或在 SQL 里写全 schema.表 后重跑")


def explain(text: str) -> str:
    """已知报错模式对应的中文提示;认不出来返回空串,由调用方决定要不要追加。"""
    low = (text or "").lower()
    for needles, hint in _HINTS:
        if needles == ("function ", " does not exist"):
            if all(n in low for n in needles):
                made = _function_hint(text)
                if made:
                    return made
            continue
        if needles == ("relation ", " does not exist"):
            if all(n in low for n in needles):
                made = _relation_hint(text)
                if made:
                    return made
            continue
        if any(n.lower() in low for n in needles):
            if needles == ("permission denied",):
                return _permission_hint(text)
            return hint
    return ""


def with_hint(message: str) -> str:
    """原文 + 换行 + 「提示:…」;认不出来就原样返回。"""
    hint = explain(message)
    return f"{message}\n提示:{hint}" if hint else message


def ensure_hint(message: str) -> str:
    """已经带过「提示:」的原样返回,否则追加——直连原始会话那条路的 DBError 没经过 runner,
    skill 的收尾处用它补一次提示,而中间件路径的 QueryError 早就带了,不会叠两次。"""
    return message if "\n提示:" in (message or "") else with_hint(message)
