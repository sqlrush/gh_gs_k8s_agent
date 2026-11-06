"""gsql 协议层（纯函数，无 I/O）：参数注入、语句判别、结果与错误解析。"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Sequence

from .base import DBError


def _inline_or_var(val: Any, idx: int, vars_: dict) -> str:
    """决定第 idx 个参数的注入形式，必要时写入 vars_。"""
    if val is None:
        return "NULL"
    if isinstance(val, bool):  # 必须在 int 之前（bool 是 int 子类）
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float, Decimal)):
        name = f"p{idx}"
        vars_[name] = str(val)
        return f":{name}"          # 裸值（数值上下文安全，值我方可控）
    if isinstance(val, str):
        name = f"p{idx}"
        vars_[name] = val
        return f":'{name}'"        # gsql 自行安全转义为带引号字面量
    raise DBError(f"unsupported gsql param type {type(val).__name__}")


def rewrite_params(sql: str, params: Sequence[Any]) -> tuple[str, dict]:
    """把 %s 占位符改写为 gsql 变量引用，返回 (新SQL, 变量映射)。

    **没有参数就原样返回，一个字符都不动。** 没参数就没有占位符要填，此时
    SQL 里的 `%` 全是用户的数据，不是我们的语法。原先无条件扫一遍的后果
    （实测 og5，直连驱动与中间件两条路都没这毛病，只有 gsql 走样）：

        LIKE 'x%y'    → Filter: (relname ~~ 'x%y')      对
        LIKE 'x%%y'   → Filter: (relname ~~ 'x%y')      **静默改写**
        LIKE 'x%sy'   → error: more %s placeholders than params

    而 DirectRunner.run() 调的正是 `db.query(sql)`，一个参数都不传 ——
    对它来说这趟扫描纯属白做工，却把 explain/proctune/sqltune 里用户给的
    `LIKE '%status%'` 这类写法弄坏了。前一种错法尤其难查：SQL 变了，
    计划是变之后那条的，没有任何地方说过一声。
    """
    params = list(params or ())
    if not params:
        return sql, {}
    out: list[str] = []
    vars_: dict = {}
    idx = 0
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "%":
            nxt = sql[i + 1] if i + 1 < n else ""
            if nxt == "%":
                out.append("%"); i += 2; continue
            if nxt == "s":
                if idx >= len(params):
                    raise DBError("more %s placeholders than params")
                out.append(_inline_or_var(params[idx], idx, vars_))
                idx += 1; i += 2; continue
            out.append("%"); i += 1; continue
        out.append(ch); i += 1
    if idx != len(params):
        raise DBError(
            f"placeholder/param count mismatch: {idx} placeholders, {len(params)} params"
        )
    return "".join(out), vars_


_LEADING_NOISE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", re.DOTALL)
_WRAPPABLE = frozenset({"SELECT", "WITH", "VALUES", "TABLE"})


def is_wrappable_select(sql: str) -> bool:
    """去掉前导空白/注释后，首关键字是否为可被 json_agg 包裹的查询。

    首关键字不在 _WRAPPABLE 集合内（如 EXPLAIN、SHOW、INSERT、SET 等）时走
    文本旁路（parse_text_result），返回逐行单元素 tuple 列表 [(line,), ...]。
    注意：EXPLAIN (FORMAT JSON) 走文本旁路时，JSON 文档会被按行切成多个 tuple，
    调用方须自行 "".join(r[0] for r in rows) + json.loads(...) 重组
    （参见 skills/gaussdb-sqltune/scripts/cost.py 的处理方式）。新探针若用
    EXPLAIN(FORMAT JSON) 必须同样处理，而非假设返回单个已解码对象。
    """
    s = _LEADING_NOISE.sub("", sql, count=1).lstrip()
    if not s:
        return False
    first = s.split(None, 1)[0].upper()
    return first in _WRAPPABLE


def wrap_select_json(sql: str) -> str:
    """把 SELECT 包成单值 JSON：列序/类型/NULL 全保真。"""
    inner = sql.strip().rstrip(";").strip()
    return f"SELECT json_agg(row_to_json(_t)) FROM ({inner}) _t"


_ERR_RE = re.compile(r"ERROR:\s+(?:([0-9A-Z]{5}):\s+)?(.*)")
# openGauss 的 VERBOSITY=verbose 把主消息挪到后续行:ERROR: 那行是空的,
# 真正的消息带 GAUSS-NNNNN 前缀,SQLSTATE 再单占一行。
_GAUSS_MSG_RE = re.compile(r"^GAUSS-\d+:\s*(.+)$")
_SQLSTATE_RE = re.compile(r"^SQLSTATE:\s*([0-9A-Z]{5})\s*$")


def parse_json_result(stdout: str) -> tuple[list[str], list[tuple]]:
    """解析 json_agg 输出为 (cols, rows)；空集 → ([], [])。"""
    text = stdout.strip()
    if not text:
        return [], []
    # **类型保真到此为止，别再往上声称更多。** 实测 openGauss-lite 5.0.3：
    #
    #   SELECT 0.5::numeric n, 0.5::float8 f, '.5'::text txt, 1e-7::float8 tiny
    #   → {"n":".5","f":".5","txt":".5","tiny":1e-07}
    #
    # 绝对值小于 1 的数被写成无前导零的 `.5`，那不是合法 JSON，于是 openGauss
    # 给它加引号 —— numeric / float4 / float8 一视同仁，而且**和真实的文本
    # 值 '.5' 完全无法区分**。所以这里不做「长得像数字就转回数字」的还原：
    # 那会把一个文本列悄悄变成数字，是拿一个静默错误换另一个。
    #
    # 影响面已实测：skill 走 runner 时结果本来就会被 serialize 成字符串，
    # 对拍 sqltune 在两条驱动下的输出，193 行里除 hypopg 那段应有的差异外
    # 逐字节一致，代价推演不受影响。会被绊到的是直接拿 db.query() 的返回值
    # 做算术的新代码 —— 那会是响亮的 TypeError，不是错误的结论。
    data = json.loads(text, parse_float=Decimal)
    if not data:                       # None 或空数组
        return [], []
    cols = list(data[0].keys())        # row_to_json 保列序，dict 保插入序
    rows = [tuple(rec.get(c) for c in cols) for rec in data]
    return cols, rows


def parse_text_result(stdout: str) -> tuple[list[str], list[tuple]]:
    """解析 -At 文本输出：每非尾空行 → 单元素 tuple。

    **列名是空的** —— 只有 query_in_rollback 那条纯文本旁路该用它，
    它的消费者要的是原始行。走 runner 的语句请用
    parse_text_result_with_header：runner 用 `if not cols` 判断
    「这条语句有没有结果集」，空列名会被判成没有。
    """
    text = stdout[:-1] if stdout.endswith("\n") else stdout
    if text == "":
        return [], []
    return [], [(line,) for line in text.split("\n")]


# gsql 不带 -t 时的末行页脚：`(2 rows)` / `(1 row)`
_ROWCOUNT_FOOTER = re.compile(r"^\(\d+ rows?\)$")


def parse_text_result_with_header(stdout: str) -> tuple[list[str], list[tuple]]:
    """解析 -A（不带 -t）文本输出：首行是列名，末行是行数页脚。

    EXPLAIN 走这条路。psycopg2 跑 EXPLAIN 会返回列名 QUERY PLAN，而 -t
    把表头去掉了，于是 gsql 返回空列名 —— runner 的 `if not cols` 把它
    判成「未返回结果集」，explain / proctune / sqltune 的 plan_text 和
    wdr 那几条在 driver: gsql 下全部跑不了。两条驱动必须给出同一形状。

    实测输出（openGauss-lite 5.0.3）：
        'QUERY PLAN\\nAggregate  (cost=...)\\n  ->  Seq Scan ...\\n(2 rows)\\n'
        'work_mem\\n16MB\\n(1 row)\\n'
    """
    text = stdout[:-1] if stdout.endswith("\n") else stdout
    if text == "":
        return [], []
    lines = text.split("\n")
    cols = lines[0].split("|")
    body = lines[1:]
    if body and _ROWCOUNT_FOOTER.match(body[-1]):
        body = body[:-1]
    if len(cols) == 1:
        # 单列时**不要**按 | 切分：执行计划里出现 | 的表达式会被切碎，
        # 而那种坏法是静默的 —— 计划少一截，没人会发现。
        return cols, [(line,) for line in body]
    return cols, [tuple(line.split("|")) for line in body]


def _verbose_parts(lines: list[str]) -> tuple[str, str]:
    """openGauss verbose 形态:从后续行里捡出主消息与 SQLSTATE。"""
    msg = code = ""
    for line in lines:
        if not msg:
            m = _GAUSS_MSG_RE.match(line.strip())
            if m:
                msg = m.group(1).strip()
                continue
        if not code:
            m = _SQLSTATE_RE.match(line.strip())
            if m:
                code = m.group(1)
    return msg, code


def parse_gsql_error(stderr: str) -> str:
    """尽量还原 'ERROR: <msg> (SQLSTATE <code>)'，否则回退原文。

    两种形态都要认:PG 式把消息写在 ERROR: 同一行;openGauss 的
    VERBOSITY=verbose 把 ERROR: 那行留空,消息挪到 GAUSS-NNNNN 行、
    SQLSTATE 另起一行。只认前者会返回光秃秃的 "ERROR: ",上层拿不到原因。
    """
    lines = stderr.splitlines()
    for i, line in enumerate(lines):
        m = _ERR_RE.search(line)
        if not m:
            continue
        code, msg = m.group(1) or "", m.group(2).strip()
        if not msg:
            msg, verbose_code = _verbose_parts(lines[i + 1:])
            code = code or verbose_code
        if not msg:
            break  # 连主消息都捞不到,回退原文比返回空 ERROR 有用
        return f"ERROR: {msg} (SQLSTATE {code})" if code else f"ERROR: {msg}"
    return stderr.strip() or "gsql failed with no error output"
