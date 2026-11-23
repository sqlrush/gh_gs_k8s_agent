"""业务库的兼容模式(v1.5.1,2026-11-20 现场反馈:M 兼容业务库上 15 条脚本报错)。

GaussDB 每个库建库时定兼容模式(pg_database.datcompatibility):A(Oracle 兼容)、M(MySQL 兼容,505.1 起)、
B(旧的 MySQL 兼容,官方不推荐)、C、PG。同一实例里 postgres 库一般是 A,业务库可能是 M —— 兼容模式跟库走,不跟实例走。

从哪来:登录时的探针 login.current_db 顺带取 compat(datcompatibility)与 lctn(lower_case_table_names);
存进会话(旁挂文件 <句柄>.compat,与切库库名 .route 同一个道理:不进会话 yaml,回滚到旧版也不报错)。
取不到(现场没导本版增量、未直连业务库)就是「未知」:各处按 A 库处理,登录输出提示导入增量。

用在哪(都只在 M 库时改变行为,A 库与 1.5.0 完全一致):
  · 白名单脚本本身写成 A / M 都能跑的同一条 SQL,不分支;只有 ADM_SOURCE 这种 M 库没有的,由调用方据此跳过;
  · 语句守卫(common/grmp/statement.py)按 M 的规则切词:# 注释、反引号、双引号字符串、反斜杠转义;
  · SQL 里取表名 / 补 schema:M 库认反引号,表名是否折小写看 lctn;
  · 登录输出与 sqltune / proctune / sqlreview 的报告写明兼容模式,模型按它写改写语句(AGENTS.md「数据库连接规则」第 5 条)。
"""
from __future__ import annotations

from typing import Any, Optional

A, B, C, M, PG = "A", "B", "C", "M", "PG"
KNOWN = (A, B, C, M, PG)
_LABELS = {
    A: "A（Oracle 兼容）",
    B: "B（MySQL 兼容·旧模式）",
    C: "C（Teradata 兼容）",
    M: "M（MySQL 兼容）",
    PG: "PG（PostgreSQL 兼容）",
}
UNKNOWN_LABEL = "未知（按 A 兼容处理）"
SUFFIX = ".compat"                     # 会话旁挂文件后缀
_ATTR_MODE, _ATTR_LCTN = "gsdb_compat", "gsdb_lctn"


def normalize(value: Any) -> str:
    """探针取回的 datcompatibility → A / B / C / M / PG;认不出(含空、NULL)→ ''(未知)。"""
    text = str(value or "").strip().upper()
    return text if text in KNOWN else ""


def normalize_lctn(value: Any) -> str:
    """lower_case_table_names → '0' / '1' / '2';取不到 → ''。"""
    text = str(value or "").strip()
    return text if text in ("0", "1", "2") else ""


def mode_of(obj: Any) -> str:
    """从连接对象(Connection.compat)或取数器(access.runner_for 打的标)取兼容模式;都没有 → ''。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return normalize(obj)
    return normalize(getattr(obj, "compat", "") or getattr(obj, _ATTR_MODE, ""))


def lctn_of(obj: Any) -> str:
    if obj is None or isinstance(obj, str):
        return ""
    return normalize_lctn(getattr(obj, "lctn", "") or getattr(obj, _ATTR_LCTN, ""))


def is_mysql_like(obj: Any) -> bool:
    """M 与 B 都按 MySQL 的词法:双引号是字符串、反引号是标识符、# 是注释、|| 是逻辑或。"""
    return mode_of(obj) in (M, B)


def is_m(obj: Any) -> bool:
    return mode_of(obj) == M


def tag(runner: Any, conn: Any) -> Any:
    """给取数器打上连接的兼容模式(取数器本身不带 Connection)。返回同一个对象,便于 return tag(...)。"""
    for attr, value in ((_ATTR_MODE, mode_of(conn)), (_ATTR_LCTN, lctn_of(conn))):
        try:
            setattr(runner, attr, value)
        except (AttributeError, TypeError):      # 个别测试替身不让设属性:当未知处理
            pass
    return runner


def label(obj: Any) -> str:
    """登录输出 / 报告里的一句话。"""
    mode = mode_of(obj)
    if not mode:
        return UNKNOWN_LABEL
    text = _LABELS[mode]
    if mode in (M, B):
        lctn = lctn_of(obj)
        if lctn == "0":
            text += "；表名区分大小写（lower_case_table_names=0）"
        elif lctn in ("1", "2"):
            text += "；表名不区分大小写（lower_case_table_names=%s）" % lctn
    return text


def report_line(obj: Any) -> str:
    """sqltune / proctune / sqlreview 报告开头那一行:只在 MySQL 词法的库(M / B)上出,A 库报告与 1.5.0 一字不差。"""
    if not is_mysql_like(obj):
        return ""
    return ("> 业务库兼容模式：%s。改写语句、建索引语句按 M 兼容写法："
            "标识符用反引号、拼接用 concat()，不用 ILIKE、||、::、双引号标识符。\n\n" % label(obj))


def folds_unquoted(obj: Any) -> bool:
    """不加引号的表名在库里是否折成小写。A / PG / 未知:是(PG 规则);M / B:lower_case_table_names=0 时否,其余是。"""
    if not is_mysql_like(obj):
        return True
    return lctn_of(obj) != "0"


def quote_ident(name: str, obj: Any) -> str:
    """按兼容模式给标识符加引号:M / B 用反引号,其余用双引号。"""
    if is_mysql_like(obj):
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def encode(mode: str, lctn: str) -> str:
    """会话旁挂文件的内容:'M 1'、'A'。"""
    mode, lctn = normalize(mode), normalize_lctn(lctn)
    return ("%s %s" % (mode, lctn)).strip() if mode else ""


def decode(text: Optional[str]):
    """旁挂文件内容 → (mode, lctn);不认识的内容当未知,不报错(这只影响写法提示,不影响连到哪个库)。"""
    parts = (text or "").split()
    mode = normalize(parts[0]) if parts else ""
    lctn = normalize_lctn(parts[1]) if len(parts) > 1 else ""
    return (mode, lctn) if mode else ("", "")
