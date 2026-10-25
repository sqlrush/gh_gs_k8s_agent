"""Catalog-driven placeholder typing for sqltune.

替换占位符前用一条已注册的 catalog 查询（sqltune.column_types）取出比较列
的真实类型，让合成值首发就类型正确。现场反复出现的
`invalid input syntax for integer: "test"` 就是列名启发式猜错整数列所致
（列名不在白名单，如 grp / uid / stock_quantity，兜底填了 'test'）。

走 runner（固定查询，注册脚本），中间件与直连两条路径通用；不依赖会话。
任何环节失败都降级回纯文本启发式并在 stderr 说一声——本模块绝不让
sqltune 因它而失败。
"""
from __future__ import annotations

import re
import sys

import evidence
import placeholder

COLUMN_TYPES_SCRIPT = "sqltune.column_types"

_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
# openGauss/GaussDB: 老式 "for integer" 与新式 "for type numeric" 两种措辞都有。
_TYPE_ERR_RE = re.compile(r'invalid input syntax for (?:type )?[\w ]+:\s*"([^"]*)"')


def _quoted_list(names: list[str]) -> str:
    """'a','b' 形态的字面量列表；括号在注册脚本的 SQL 里（与 tables.yaml 同约定）。"""
    return ",".join("'" + n.replace("'", "''") + "'" for n in names)


def infer_types(runner, sql_text: str, schema: str = "") -> list:
    """Per-placeholder column type (or None), aligned with substitute() order.

    列名取自各占位符的左上下文；同名列在多张表里类型冲突时保守放弃
    （返回 None，交回启发式）。查询失败同样全量降级。
    知道 SQL 所在 schema 时先把别的 schema 里同名表的列剔掉(common/catalog_scope.py):
    现场同名表跨 schema,不剔的话类型「冲突」就放弃,合成 'test' 塞进数值列又是一个 400。
    """
    contexts = placeholder.placeholder_contexts(sql_text)
    if not contexts:
        return []
    columns = [placeholder.comparison_column(c) for c in contexts]
    wanted = sorted({c for c in columns if c})
    tables = evidence.extract_tables(sql_text)
    if not wanted or not tables:
        return [None] * len(contexts)

    try:
        rows = runner.run(COLUMN_TYPES_SCRIPT,
                          {"tables": _quoted_list(tables),
                           "columns": _quoted_list(wanted)})
        from common import catalog_scope as cs
        from common import search_path as sp
        # schema 可能是搜索路径串(`fl, public`):按第一段过滤,理由同 evidence.collect
        scope = cs.Scope(explicit=cs.explicit_schemas(evidence.extract_table_refs(sql_text)),
                         default=sp.primary_schema(schema))
        rows = cs.keep_rows(rows, scope, table_key="table_name", schema_key="schema_name")
        mapping = _unambiguous((r["attname"], r["type_name"]) for r in rows)
    except Exception as exc:  # 探测是增强,不是硬依赖——失败就退回启发式
        print(f"warning: 列类型探测失败,占位符替换退回启发式: {exc}",
              file=sys.stderr)
        return [None] * len(contexts)
    return [_operand_type(ctx, mapping.get(c)) if c else None for ctx, c in zip(contexts, columns)]


def _operand_type(left_ctx: str, column_type):
    """比较位就是列的类型;算术位(`c.grade + ?`)换成运算另一侧该有的类型。"""
    if placeholder.is_arithmetic_context(left_ctx):
        return placeholder.arithmetic_operand_type(column_type)
    return column_type


def _unambiguous(pairs) -> dict:
    """只保留类型无歧义的列;跨表同名冲突宁可不猜。"""
    seen: dict = {}
    for col, typ in pairs:
        seen.setdefault(col, set()).add(typ)
    return {col: next(iter(types)) for col, types in seen.items() if len(types) == 1}


def _unquote(value: str) -> str:
    """剥掉最外层 SQL 单引号,让 `--bind "'2'"` 这种老写法照常过数值校验。

    2026-08-14 之前脚本不会自己加引号,现场模型学到的唯一可行写法就是把引号
    写进值里；接口改进不该把老用法判成错位。`'L1'` 剥完仍不是数字,照拦。
    """
    v = value.strip()
    if len(v) >= 2 and v.startswith("'") and v.endswith("'"):
        return v[1:-1].replace("''", "'")
    return v


def validate_binds(substitutions, types: list) -> None:
    """--bind 的值与推断类型明显不符时执行前拦截（治 bind 顺序错位）。

    只校验整数/数值族——字符串塞进整数列必炸且报错难定位；日期等格式
    多样，不硬卡。
    """
    problems = []
    for i, s in enumerate(substitutions):
        if s.source != "bind":
            continue
        t = types[i] if i < len(types) else None
        if t and placeholder.is_numeric_type(t) \
                and not _NUMERIC_LITERAL_RE.match(_unquote(s.value)):
            problems.append(
                f"  bind #{i + 1} = {s.value!r} 但该占位符对应 {t} 列"
                f"（上下文: …{s.context[-50:]}）")
    if problems:
        raise ValueError(
            "bind 值与占位符类型不符——检查 --bind 顺序是否错位:\n"
            + "\n".join(problems))


# 降级用:报错里点名了目标类型 / 运算符两侧类型
_TYPE_ERR_TARGET_RE = re.compile(r'invalid input syntax for (?:type )?([\w ]+?):\s*"([^"]*)"')
_OPERATOR_ERR_RE = re.compile(r"operator does not exist: ([\w ]+?) (?:=|<>|!=|<=|>=|<|>) ([\w ]+)")
_SYNTHETIC_SOURCES = frozenset({"rule", "rule-format-followup", "default"})
_CMP_TAIL_RE = re.compile(r"(?:=|<>|!=|<=|>=|<|>|\bin\s*\([^()]*)\s*$", re.I)


def _quoted(value: str) -> str:
    return value if value.startswith("'") else "'" + value.replace("'", "''") + "'"


def fallback_for_error(message: str, substitutions):
    """EXPLAIN 因合成值类型不对被拒时,给出「换成什么能过」:{占位符下标: 新值};不适用返回 None。

    两类报错,都只动 sqltune 自己猜的值(bind 是用户给的,错了要让用户知道,不替他改):
      · invalid input syntax for <类型>: "<值>" —— 那个位置接受未定类型的字面量、按 <类型> 解析失败。
        换成 <类型> 解析得了的带引号字面量('1'、'2024-01-01'),这一处就一定能过;
        同值的其他位置一起换,'1' 落在字符串列上也照样合法。
      · operator does not exist: character varying = integer —— 按列名猜成整数的裸数字撞上了字符串列。
        比较位上的裸数字一律加引号:'1' 对整数列、字符串列都成立。
    换出来的值只保证计划生成得了,选择率按它估 —— 报告里要说明(见 placeholder.with_values 的 note)。
    """
    m = _TYPE_ERR_TARGET_RE.search(message or "")
    if m:
        target, bad = m.group(1).strip(), m.group(2)
        hits = [i for i, s in enumerate(substitutions) if s.value == bad or s.value.strip("'") == bad]
        literal = placeholder.value_for_type(target)
        if not hits or literal is None or any(substitutions[i].source == "bind" for i in hits):
            return None
        return {i: _quoted(literal) for i in hits}
    m = _OPERATOR_ERR_RE.search(message or "")
    if m and any(placeholder.is_numeric_type(t) for t in m.groups()):
        hits = {i: _quoted(s.value) for i, s in enumerate(substitutions)
                if s.source in _SYNTHETIC_SOURCES and _NUMERIC_LITERAL_RE.match(s.value)
                and _CMP_TAIL_RE.search(s.context)}
        return hits or None
    return None


def fallback_note(message: str, substitutions, replacements: dict) -> str:
    """报告里那一句:哪个占位符的哪个值被拒、按什么报错换成了什么。

    只引报错里的那一句(invalid input syntax … / operator does not exist …):中间件的报错前缀
    带 task_id 之类的内部标识,不进报告。"""
    m = _TYPE_ERR_TARGET_RE.search(message or "") or _OPERATOR_ERR_RE.search(message or "")
    why = m.group(0) if m else "类型不符"
    parts = ["#%d %s → %s" % (i + 1, substitutions[i].value, v) for i, v in sorted(replacements.items())]
    return ("首个合成值被数据库拒绝(%s),已改填 %s 重取计划 —— 这几个位置的列类型没推断出来,"
            "改填的值只保证计划能生成,行数与选择率按它估。" % (why, "、".join(parts)))


def enrich_type_error(message: str, substitutions):
    """EXPLAIN 报类型转换错时,点名坏值出自哪个占位符;不相关则返回 None。"""
    m = _TYPE_ERR_RE.search(message)
    if not m:
        return None
    bad = m.group(1)
    hits = [(i, s) for i, s in enumerate(substitutions)
            if s.value == bad or s.value.strip("'") == bad]
    if not hits:
        return None

    lines = [f"  #{i + 1} {s.token} -> {s.value} ({s.source})"
             f"  上下文: …{s.context[-50:]}" for i, s in hits]
    if all(s.source == "bind" for _, s in hits):
        hint = ("提示: 该值来自 --bind,疑似 bind 顺序错位——"
                "对照下列位置检查传值顺序:")
    else:
        hint = ("提示: 该值是 sqltune 自动填的合成值,列类型猜错了。"
                "若用户能给出真实值,可用 --bind 按占位符顺序传入绕过猜测"
                "(没有就不要臆造——编出来的值会改变选择性,索引/改写的 cost "
                "倍数会跟着失真)。可能位置:")
    return message + "\n" + hint + "\n" + "\n".join(lines)
