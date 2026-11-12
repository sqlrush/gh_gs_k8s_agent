"""EXPLAIN 前把 search_path 切到 SQL 原本的 schema。

现场 sqltune 报 HTTP 400 的成因(2026-09-07 反馈「入参 sql 没带 schema」):业务 SQL 的表名多半不带 schema,
靠应用账号的 search_path 解析;EXPLAIN 是用中间件执行账号跑的,它的 search_path 是 "$user", public,
解析不到 → relation does not exist → 中间件包成 400。而 statement_history 里本来就记着 schema_name。

两条路:
  · 白名单模板(中间件与直连都走这条):把 SQL 里不带 schema 的表名按 schema 补全,再用单语句模板取计划。
    原先先发一条两语句探测、能跑就用「SET search_path …; EXPLAIN …」模板 —— 行方中间件拦截一条脚本里的两段 SQL
    (2026-11-12 现场),探测每次对话都撞一次拦截,那 8 条两语句脚本在行方永远跑不成,v1.4.13 起不再交付。
  · 直连原始会话:直接在会话上 SET search_path,后面的 EXPLAIN 与 hypopg 都受益(set_search_path)。

schema 名同时进 SET 语句的引号里:不是合法标识符一律不用——中间件是文本替换,这里就是注入面。

statement_history.schema_name 记的其实是会话的**搜索路径串**,不是单个 schema(2026-10-21 现场:`fl, public`,
sqltune 报「schema 名 'fl, public' 不是合法标识符」整条没切)。所以入参一律按搜索路径处理:按逗号拆、去空白,
每一段单独过 IDENT_RE(规则不放宽),各自加引号后整体设为 search_path;任何一段不合法(含空段、带引号的
"$user")整串不切,说明里点出是哪一段——不挑能用的几段拼一个「差不多」的路径,那会悄悄改变表名解析。
单个 schema 就是只有一段的路径,行为与原先逐字一致。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
FALLBACK_SCHEMA = "public"        # 会话 SET 在末尾补的兜底段(`"<schema>", public`)


def valid_schema(name: Any) -> bool:
    return isinstance(name, str) and bool(IDENT_RE.match(name))


def split_search_path(text: Any) -> List[str]:
    """搜索路径串 → 各段(去两端空白,原样大小写)。空段留成空串,交给 parse_search_path 判不合法。"""
    if not isinstance(text, str) or not text.strip():
        return []
    return [seg.strip() for seg in text.split(",")]


def parse_search_path(text: Any) -> Tuple[List[str], Optional[str]]:
    """(各段, 第一个不合法的段)。全部合法时第二项是 None;空串 / 非字符串 → ([], 原值)。

    不合法时第一项返回空列表:调用方拿不到「部分合法」的段,也就不会拼出半截路径。
    """
    segs = split_search_path(text)
    if not segs:
        return [], ("" if text is None else str(text))
    for seg in segs:
        if not valid_schema(seg):
            return [], seg
    return segs, None


_QUOTED_RE = re.compile(r'^"([^"]*)"$')


def business_path(text: Any) -> str:
    """SQL 历史里记的搜索路径 → 能用来找业务表的那几段(2026-11-06 行方:技能不要做跨 schema 的限制)。

    statement_history.schema_name 记的是业务会话的搜索路径原文,默认就是 `"$user",public`。原先整串按「带引号、
    不合法」拒掉、一段都不切,业务表就找不到。这里:`"$user"` 去掉(它指的是业务账号自己,换成中间件执行账号
    就指错了);合法标识符外的一对引号去掉;只剩 public(或什么都不剩)时返回空串 —— 调用方按表名在目录里找 schema。
    其余不合法的段原样留着,照旧由 parse_search_path 判掉(那是注入防线,不是 schema 限制)。
    """
    segs = []
    for seg in split_search_path(text):
        m = _QUOTED_RE.match(seg)
        inner = m.group(1) if m else seg
        if inner.lower() == "$user":
            continue
        segs.append(inner if m and valid_schema(inner) else seg)
    if not segs or segs == [FALLBACK_SCHEMA]:
        return ""
    return ", ".join(segs)


def valid_search_path(text: Any) -> bool:
    """单个 schema 或逗号分隔的搜索路径,每一段都是合法标识符。命令行 --schema 与 sql_id 取回的值都用它判。"""
    return parse_search_path(text)[1] is None


def primary_schema(text: Any) -> str:
    """搜索路径的第一段:补全表名、目录行按 schema 过滤、「表名写全为 x.<表>」用它。

    不带 schema 的表名按路径顺序解析,业务表绝大多数落在第一段(public 一般垫底);只作展示 / 过滤时
    不校验,真要拼进 SQL 的地方(sqlqualify)自己会再校验。
    """
    segs = split_search_path(text)
    return segs[0] if segs else ""


def dba_search_path(text: Any) -> str:
    """给 DBA 照抄的 `SET search_path = …` 右边那串:各段原样,路径里没有 public 才补(原先是 `<schema>, public`)。"""
    segs = [s for s in split_search_path(text) if s]
    return ", ".join(segs if FALLBACK_SCHEMA in segs else segs + [FALLBACK_SCHEMA])


def _head_segments(segs: List[str]) -> List[str]:
    """去掉末尾那段 public:模板与会话 SET 都会自己补 `, public`,`fl, public` 因此渲染成与单 schema
    完全相同的 `"fl", public`(现场验过的那条),不多出一段重复的 public。只有 public 一段时保留,免得引号里为空。"""
    if len(segs) > 1 and segs[-1] == FALLBACK_SCHEMA:
        return segs[:-1]
    return segs


def template_value(segs: List[str]) -> str:
    """填进 `SET search_path TO "{{schema}}", public` 的值。

    模板是文本替换、外层那对引号在模板里:多段时用常量 `", "` 连接,渲染成 `"fl", "app", public`。
    能这样拼的前提是每一段都过了 IDENT_RE(不含引号、分号、空白)——调用方必须先过 parse_search_path。
    """
    for seg in segs:
        if not valid_schema(seg):              # 防御:绕过 parse_search_path 直接调用时也不放行
            raise ValueError("schema 名 %r 不是合法标识符,拒绝拼进 search_path 模板" % (seg,))
    return '", "'.join(_head_segments(segs))


def _invalid_reason(text: Any, bad: Optional[str]) -> str:
    """不合法时的说法:单个 schema 沿用原先的「schema 名 'x' 不是合法标识符」;路径串点出是哪一段坏了。"""
    if len(split_search_path(text)) <= 1:
        return "schema 名 %r 不是合法标识符" % (text,)
    return "search_path %r 里的 schema 名 %r 不是合法标识符(整串不切)" % (text, bad)


def _join(rows: List[Dict[str, Any]]) -> str:
    return "\n".join(str(next(iter(r.values()), "")) for r in (rows or []) if isinstance(r, dict))


def explain_plan(runner, base_script: str, sql: str, schema: str) -> Tuple[str, str, str]:
    """走注册模板取计划。返回 (计划文本, 实际切到的搜索路径, 说明)。

    schema 是单个 schema 或搜索路径串(`fl, public`)。为空 → 原 SQL 走单语句模板;各段合法 → 把不带 schema 的表名
    补全后走单语句模板(说明里写清补了哪些名字);不合法 → 原 SQL,说明里写清原因。实际切到的路径一律返回空串
    (模板路径不再切 search_path)。
    """
    if not schema:
        return _join(runner.run(base_script, {"sql": sql})), "", ""
    segs, bad = parse_search_path(schema)
    if bad is not None:
        return (_join(runner.run(base_script, {"sql": sql})), "",
                "%s,未切换 search_path;表名不带 schema 时 EXPLAIN 可能报对象不存在" % _invalid_reason(schema, bad))
    path = ", ".join(segs)
    # 不切 search_path(行方中间件拦截两段 SQL):先把 SQL 里不带 schema 的表名补全再走单语句模板——
    # 不依赖执行器、不依赖 DBA;补全后解析到同一张表,计划与原 SQL 等价。没有可补的才原样退回。
    # 路径有多段时按第一段补(业务表绝大多数在第一段,public 垫底),说明里如实写这个前提。
    from common import sqlqualify           # 延迟导入:sqlqualify 反过来引用本模块的 valid_schema
    qualified, changed = sqlqualify.qualify_tables(sql, segs[0])
    note = qualified_note(path, changed) if changed else fallback_note(path)
    try:
        rows = runner.run(base_script, {"sql": qualified if changed else sql})
    except Exception as exc:               # noqa: BLE001
        if changed:
            # 补全出来的 SQL 执行失败:先用原 SQL 再试一次 —— 补全规则漏了某种语法时(2026-11-08 行方报告 Bug 1 那类),
            # 原 SQL 往往本来就能执行(表在执行账号的 search_path 里),不该因为这一步把整条调优搞挂
            try:
                rows = runner.run(base_script, {"sql": sql})
            except Exception:              # noqa: BLE001 —— 原 SQL 也不行:报补全后的那个错,说明照旧
                pass
            else:
                return _join(rows), "", ("%s;补全表名后的 SQL 执行失败(%s),已改用原 SQL 取计划"
                                         % (fallback_note(path), str(exc).splitlines()[0][:120]))

        # 退回单语句模板后 EXPLAIN 又失败:这时没有报告可以放说明,把原因和 DBA 命令直接跟在报错后面——
        # 用户看到的就是这一段。
        try:
            wrapped = type(exc)("%s\n补充:%s" % (exc, note))
        except Exception:                  # noqa: BLE001 —— 异常类构造签名特殊时保留原报错
            raise exc
        raise wrapped from exc
    return _join(rows), "", note


def qualified_note(schema: str, changed) -> str:
    """补全表名后取计划时的说明:补了哪些名字、计划等价。不再给 DBA 改执行账号 search_path 的命令 ——
    生产环境权限与配置固定(2026-11-12 行方),能调的只有发出去的 SQL。

    schema 是搜索路径时按第一段补全:只有表确实在第一段下才与原 SQL 等价,这个前提要写出来,不笼统说「等价」。
    """
    first = primary_schema(schema)
    names = "、".join("%s.%s" % (first, n) for n in changed)
    if len(split_search_path(schema)) > 1:
        return ("已把 SQL 里不带 schema 的表名按搜索路径的第一个 schema %s 补全后取计划(%s);表在 %s 下时"
                "解析到同一张表,计划与原 SQL 等价,若表其实在路径后面的 schema 里,EXPLAIN 会报对象不存在"
                % (first, names, first))
    return "已把 SQL 里不带 schema 的表名按 %s 补全后取计划(%s),解析到同一张表,计划与原 SQL 等价" % (first, names)


def fallback_note(schema: str) -> str:
    """SQL 里没有可补全的表名(或补全后仍失败)时给用户看的话:能调的只剩 SQL 本身。"""
    return ("SQL 里的表名没法按 %s 自动补全;表名不带 schema 时 EXPLAIN 可能报对象不存在。"
            "可以把 SQL 里的表名写全为 %s.<表> 后重跑;仍取不到时这条 SQL 的计划无法获取,不影响其余部分的分析"
            % (schema, primary_schema(schema)))


def set_search_path(db, schema: str) -> str:
    """直连原始会话:SET search_path TO "<schema>"[, "<schema2>" …], public。返回实际切到的路径(空串 = 没切)。

    schema 可以是搜索路径串;任何一段不合法整串拒绝(ValueError),调用方把原因写进说明。
    """
    if not schema:
        return ""
    segs, bad = parse_search_path(schema)
    if bad is not None:
        raise ValueError("%s,拒绝拼进 SET search_path" % _invalid_reason(schema, bad))
    quoted = ", ".join('"%s"' % s for s in _head_segments(segs))
    db.execute("SET search_path TO %s, %s" % (quoted, FALLBACK_SCHEMA))
    return ", ".join(segs)


__all__ = ["IDENT_RE", "FALLBACK_SCHEMA", "valid_schema", "split_search_path",
           "business_path", "parse_search_path", "valid_search_path", "primary_schema", "dba_search_path", "template_value",
           "explain_plan", "set_search_path", "fallback_note", "qualified_note"]
