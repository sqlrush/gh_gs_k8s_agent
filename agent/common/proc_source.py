"""取存储过程源码:先 pg_proc,取不到再 ADM_SOURCE —— procinfo / proctune 共用。

为什么要有 ADM_SOURCE 这一路(客户 2026-10-29 需求):开了对象隔离(ENABLE PRIVATE OBJECT)的库,数据库给
PG_PROC、PG_NAMESPACE 加了行级访问控制,普通账号联表查询 0 行(2026-09-08 现场:过程明明在,skill 说找不到)。
ADM_SOURCE 是管理员视角的视图,执行账号能读它(系统管理员,或 DBA 授了权)时看得到全部定义。

视图的形状决定了这里的几条规矩(字段 owner / name / type / line / text,一行源码一条记录):
  · 没有 schema 列,只有 owner。给了 schema 时取 owner 同名的那份;都对不上而全库只有一份同名对象,就用它,
    并在来源说明里写明所有者;不止一份又对不上 → 拒绝并列出候选,不猜。
  · 没有形参与对象 id:同名重载的几份会混在一起(同一行号出现两次)→ 拒绝。
  · 包内过程不单独成行:取整个 package body,按 PROCEDURE / FUNCTION 声明与配对的 END 切出目标过程;
    包级声明(变量、类型、游标)一并带回,报告里当上下文。
  · 每行 text 末尾带不带换行、A 兼容库里空行是不是 NULL,都不假设:逐行去掉行尾换行再拼,缺的行号补空行,
    拼出来的第 N 行就是 ADM_SOURCE 的第 N 行。
  · 过程头(PROCEDURE p(…) IS)换成 DECLARE、行数不变 —— 与 pg_proc.prosrc 同形,静态分析照常跑;形参取自过程头。

这里不拼 SQL:runner 只跑登记好的 <skill>.proc_def 与 <skill>.source_adm。
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common import procname
from common.grmp.errors import QueryError

MAX_LINES = 20000                   # 与 <skill>.source_adm 的 LIMIT 20001 配套:多出那一行就是「超限」
_STANDALONE = ("procedure", "function")
_PKG_BODY = "package body"
_DECLS_MAX_LINES = 200              # 报告里包级声明最多带这么多行:够看清引用到的包级变量与游标,又不把报告撑爆
_DENIED = re.compile(r"permission denied|insufficient privilege|42501|权限", re.I)


@dataclass(frozen=True)
class Resolved:
    """一个过程的源码与出处。body 与 pg_proc.prosrc 同形(DECLARE … BEGIN … END),args 是形参原文。"""
    schema: str
    package: str
    name: str
    lang: str
    body: str
    args: str
    origin: str = "pg_proc"         # pg_proc | adm_source
    note: str = ""                  # 给报告的来源说明;pg_proc 正常取到时为空
    context: str = ""               # 包级声明原文(只有从 ADM_SOURCE 切出包内过程时有)


class Unavailable(Exception):
    """ADM_SOURCE 这条路走不通:脚本没登记、执行账号没权限、库里没有这个视图……reason 只留第一句。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.denied = bool(_DENIED.search(reason))


# ---------------------------------------------------------------- 入口

def resolve(runner, def_script: str, adm_script: str, qualified_name: str) -> Resolved:
    """先按 <skill>.proc_def 查 pg_proc;查不到、或查到了过程体却是空的,再按 <skill>.source_adm 查 ADM_SOURCE。

    两边都没有 → procname.NotFound(带 adm 说明,排查报告据此提示授权);有但不唯一 → ValueError,不猜。
    """
    try:
        row = procname.lookup(runner, def_script, qualified_name)
    except procname.NotFound as nf:
        return _adm_or_raise(runner, adm_script, nf)
    res = Resolved(schema=_s(row.get("nspname")), package=_s(row.get("package")), name=_s(row.get("proname")),
                   lang=_s(row.get("lanname")), body=str(row.get("prosrc") or ""), args=_s(row.get("args")))
    if res.body.strip():
        return res
    # pg_proc 有这一行、过程体却是空的(对象隔离下没有执行权限的过程,定义可能不给看)
    why = "pg_proc 里这个过程的源码为空"
    try:
        src = fetch(runner, adm_script, procname.ProcRef(res.schema, res.package, res.name))
    except Unavailable as exc:
        return dataclasses.replace(res, note="%s;ADM_SOURCE 也取不到(%s)" % (why, exc.reason))
    if src is None:
        return dataclasses.replace(res, note="%s;ADM_SOURCE 里也没有" % why)
    return dataclasses.replace(src, note="%s,%s" % (why, src.note))


def _adm_or_raise(runner, adm_script: str, nf: procname.NotFound) -> Resolved:
    try:
        src = fetch(runner, adm_script, nf.ref)
    except Unavailable as exc:
        raise procname.NotFound(nf.ref, nf.tried, str(nf), adm="没取到 —— %s" % exc.reason,
                                adm_denied=exc.denied) from None
    if src is None:
        raise procname.NotFound(nf.ref, nf.tried, str(nf),
                                adm="也没有(按 %s 查过)" % _adm_tried(nf.ref)) from None
    return dataclasses.replace(src, note="pg_proc 里查不到,%s" % src.note)


def _adm_tried(ref: procname.ProcRef) -> str:
    if ref.package:
        return "包 %s 的包体" % ref.package
    if ref.schema:
        return "过程 / 函数 %s、包 %s 的包体" % (ref.name, ref.schema)
    return "过程 / 函数 %s" % ref.name


def fetch(runner, script: str, ref: procname.ProcRef) -> Optional[Resolved]:
    """从 ADM_SOURCE 取 ref 指的过程。None = 视图里也没有;Unavailable = 这条路走不通;ValueError = 不唯一 / 切不出来。"""
    if ref.package:
        return _from_package(runner, script, ref.schema, ref.package, ref.name)
    groups = _groups(_query(runner, script, ref.name), _STANDALONE)
    if ref.schema:
        exact = {k: v for k, v in groups.items() if k[0].lower() == ref.schema.lower()}
        if exact:
            return _standalone(ref, exact)
        # 两段名 a.b 也可能是 package.proc —— 与 pg_proc 那一路同一个顺序
        hit = _from_package(runner, script, "", ref.schema, ref.name)
        if hit is not None:
            return hit
    return _standalone(ref, groups) if groups else None


def _standalone(ref: procname.ProcRef, groups: Dict[Tuple[str, str], List[Tuple[int, str]]]) -> Resolved:
    (owner, kind), lines = _pick(groups, ref.schema, ref.name)
    text = assemble(lines, "%s.%s" % (owner, ref.name))
    args, body, lang = normalize(text)
    return Resolved(schema=ref.schema or owner, package="", name=ref.name, lang=lang, body=body, args=args,
                    origin="adm_source",
                    note="源码取自 ADM_SOURCE:%s %s,共 %d 行%s" % (
                        _KIND_CN.get(kind, kind), ref.name, len(text.splitlines()), _owner_note(ref.schema, owner)))


def _from_package(runner, script: str, schema: str, package: str, name: str) -> Optional[Resolved]:
    groups = _groups(_query(runner, script, package), (_PKG_BODY,))
    if not groups:
        return None
    (owner, _kind), lines = _pick(groups, schema, package)
    text = assemble(lines, "%s.%s" % (owner, package))
    decls, spans = package_subprograms(text)
    hits = [sp for sp in spans if sp.name == name]
    if not hits:
        have = "、".join(sorted({sp.name for sp in spans})[:20]) or "(没有切出任何子程序)"
        raise ValueError("ADM_SOURCE 里包 %s 的包体中没有名为 %s 的过程 / 函数。包体里有:%s。请核对过程名。"
                         % (package, name, have))
    if len(hits) > 1:
        where = "、".join("第 %d–%d 行" % (_line(text, sp.start), _line(text, sp.end - 1)) for sp in hits)
        raise ValueError("包 %s 里 %s 有 %d 个同名重载(包体 %s)。ADM_SOURCE 不带形参,分不出要看哪一个 —— 不猜;"
                         "请把要看的那一份的形参告诉 DBA,由 DBA 确认后单独提供源码。" % (package, name, len(hits), where))
    sp = hits[0]
    first, last = _line(text, sp.start), _line(text, sp.end - 1)
    args, body, lang = normalize(text[sp.start:sp.end])
    return Resolved(schema=schema or owner, package=package, name=name, lang=lang, body=body, args=args,
                    origin="adm_source", context=_cap_lines(decls.strip("\n"), _DECLS_MAX_LINES),
                    note="源码取自 ADM_SOURCE:包 %s 的包体第 %d–%d 行(下面过程体的第 1 行即包体第 %d 行)%s"
                         % (package, first, last, first, _owner_note(schema, owner)))


_KIND_CN = {"procedure": "过程", "function": "函数"}


def _owner_note(schema: str, owner: str) -> str:
    if not schema or schema.lower() == owner.lower():
        return ";所有者 %s" % owner
    return (";ADM_SOURCE 只给出所有者 %s(与给出的 schema %s 不同),全库只有这一份同名对象,按它分析"
            % (owner, schema))


# ---------------------------------------------------------------- 取数与拼接

def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _int(v: Any) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        raise ValueError("ADM_SOURCE 返回的行号 %r 不是数字" % (v,)) from None


_DB_ERR = re.compile(r"ERROR:\s*(.+?)(?:\s+on\s+\S+)?(?:\s*\(SQLSTATE\s*\w+\))?\s*$")


def _reason(exc: Exception) -> str:
    """失败原因说成一句人话,括号里带数据库原文。报告给用户与 DBA 看:中间件的任务号、全部已注册脚本名都不要。"""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    if "未注册脚本" in first:
        return "中间件里还没有登记这条脚本(按交付说明导入本版白名单增量后可用)"
    m = _DB_ERR.search(first)
    raw = (m.group(1) if m else first.split("。", 1)[0])[:160]
    if _DENIED.search(first):
        return "执行账号没有 ADM_SOURCE 的读权限(%s)" % raw
    if re.search(r"does not exist|不存在", raw):
        return "本库没有 ADM_SOURCE 视图(%s)" % raw
    return raw


def _query(runner, script: str, name: str) -> List[Dict[str, Any]]:
    try:
        rows = runner.run(script, {"name": name})
    except QueryError as exc:
        raise Unavailable(_reason(exc)) from exc
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if len(rows) > MAX_LINES:
        raise ValueError("ADM_SOURCE 里 %s 的源码超过 %d 行,不做分析 —— 截断的过程体分析出来的结论不可靠。"
                         "请 DBA 直接导出源码另行提供。" % (name, MAX_LINES))
    return rows


def _groups(rows: Sequence[Dict[str, Any]], kinds: Sequence[str]) -> Dict[Tuple[str, str], List[Tuple[int, str]]]:
    """按 (owner, type) 归组,只留 kinds 里的类型。"""
    out: Dict[Tuple[str, str], List[Tuple[int, str]]] = {}
    for r in rows:
        kind = _s(r.get("type")).lower()
        if kind in kinds:
            out.setdefault((_s(r.get("owner")), kind), []).append((_int(r.get("line")), str(r.get("text") or "")))
    return out


def _pick(groups: Dict[Tuple[str, str], List[Tuple[int, str]]], schema: str, name: str):
    keys = sorted(groups)
    exact = [k for k in keys if schema and k[0].lower() == schema.lower()]
    chosen = exact if exact else keys
    if len(chosen) == 1:
        return chosen[0], groups[chosen[0]]
    cands = "、".join("%s.%s(%s)" % (o, name, _KIND_CN.get(t, t)) for o, t in chosen)
    raise ValueError("ADM_SOURCE 里叫 %s 的对象不止一份:%s。请用 schema.proc 或 schema.package.proc 指明是哪一个 —— 不猜,"
                     "猜错就是在分析另一个对象,而输出看起来完全正常。" % (name, cands))


def assemble(lines: Sequence[Tuple[int, str]], what: str = "") -> str:
    """(行号, text) → 源码全文。第 N 行就是 ADM_SOURCE 的第 N 行;同一行号出现两次 = 同名重载混在一起,拒绝。"""
    if not lines:
        return ""
    by: Dict[int, str] = {}
    for n, text in lines:
        if n in by:
            raise ValueError("ADM_SOURCE 里 %s 的第 %d 行出现了不止一次 —— 有同名重载,ADM_SOURCE 不带形参,分不出"
                             "要看哪一个,不猜。请 DBA 确认要看的是哪一份后单独提供源码。" % (what or "这个对象", n))
        by[n] = text.rstrip("\r\n")
    lo, hi = min(1, min(by)), max(by)
    return "\n".join(by.get(i, "") for i in range(lo, hi + 1)) + "\n"


def _line(text: str, offset: int) -> int:
    """偏移 offset 处的字符在第几行(从 1 起)。"""
    return text.count("\n", 0, offset) + 1


def _cap_lines(text: str, n: int) -> str:
    rows = text.splitlines()
    if len(rows) <= n:
        return text
    return "\n".join(rows[:n]) + "\n-- ……(包级声明共 %d 行,这里只列前 %d 行)" % (len(rows), n)


# ---------------------------------------------------------------- 词法:只认词、带引号的名字、标点;跳过字符串与注释

@dataclass(frozen=True)
class _Tok:
    kind: str       # w 词(已小写) | q 带双引号的名字 | d 美元引号块 | p 标点
    text: str
    start: int
    end: int


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _skip_quoted(s: str, i: int, q: str) -> int:
    n, i = len(s), i + 1
    while i < n:
        if s[i] == q:
            if i + 1 < n and s[i + 1] == q:        # '' / "" 是转义
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _tokens(s: str) -> List[_Tok]:
    out: List[_Tok] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "'":
            i = _skip_quoted(s, i, "'")
        elif c == '"':
            j = _skip_quoted(s, i, '"')
            out.append(_Tok("q", s[i + 1:max(i + 1, j - 1)].replace('""', '"').lower(), i, j))
            i = j
        elif c == "$" and _DOLLAR.match(s, i):
            tag = _DOLLAR.match(s, i).group(0)
            j = s.find(tag, i + len(tag))
            j = n if j < 0 else j + len(tag)
            out.append(_Tok("d", tag, i, j))
            i = j
        else:
            m = _WORD.match(s, i)
            if m:
                out.append(_Tok("w", m.group(0).lower(), i, m.end()))
                i = m.end()
            else:
                out.append(_Tok("p", c, i, i + 1))
                i += 1
    return out


def _is(t: Optional[_Tok], kind: str, *texts: str) -> bool:
    return t is not None and t.kind == kind and (not texts or t.text in texts)


def _at(toks: List[_Tok], i: int) -> Optional[_Tok]:
    return toks[i] if 0 <= i < len(toks) else None


@dataclass(frozen=True)
class _Header:
    kind: str           # procedure | function
    name: str
    args: str
    is_idx: int         # IS / AS 的下标;-1 = 前置声明(以分号结束,没有过程体)
    end_idx: int        # IS / AS 或分号的下标


def _header(s: str, toks: List[_Tok], i: int) -> Optional[_Header]:
    """toks[i] 是 PROCEDURE / FUNCTION:读名字(可带 schema. / 包名. 前缀)、括号里的形参,一直读到 IS / AS 或分号。"""
    kind, j, name = toks[i].text, i + 1, ""
    while _is(_at(toks, j), "w") or _is(_at(toks, j), "q"):
        name = toks[j].text
        if _is(_at(toks, j + 1), "p", "."):
            j += 2
            continue
        j += 1
        break
    if not name:
        return None
    args = ""
    if _is(_at(toks, j), "p", "("):
        k = _close_paren(toks, j)
        args = s[toks[j].end:toks[k].start] if k < len(toks) else s[toks[j].end:]
        j = k + 1
    depth = 0
    while j < len(toks):
        t = toks[j]
        if _is(t, "p", "("):
            depth += 1
        elif _is(t, "p", ")"):
            depth -= 1
        elif depth == 0 and _is(t, "p", ";"):
            return _Header(kind, name, args.strip(), -1, j)
        elif depth == 0 and _is(t, "w", "is", "as"):
            return _Header(kind, name, args.strip(), j, j)
        j += 1
    return None


def _close_paren(toks: List[_Tok], j: int) -> int:
    depth = 0
    for k in range(j, len(toks)):
        if _is(toks[k], "p", "("):
            depth += 1
        elif _is(toks[k], "p", ")"):
            depth -= 1
            if depth == 0:
                return k
    return len(toks)


# ---------------------------------------------------------------- 规整:过程头 → DECLARE

def normalize(text: str) -> Tuple[str, str, str]:
    """独立过程 / 函数的完整定义 → (形参原文, 与 prosrc 同形的过程体, 语言)。

    认得的形态:`[CREATE [OR REPLACE]] PROCEDURE|FUNCTION 名[(形参)] [RETURN …] IS|AS …`(Oracle 风格,头换成
    DECLARE、行数不变),`… AS $$ … $$ [LANGUAGE x]`(取美元引号里的那段,前面补空行保住行号)。
    认不出过程头就原样返回 —— 那本来就是 prosrc 的样子。
    """
    toks = _tokens(text)
    i = 0
    if _is(_at(toks, 0), "w", "create"):
        i = 1
        while i < min(len(toks), 8) and not _is(toks[i], "w", "procedure", "function"):
            i += 1
    if not _is(_at(toks, i), "w", "procedure", "function"):
        return "", text, "plpgsql"
    h = _header(text, toks, i)
    if h is None or h.is_idx < 0:
        return "", text, "plpgsql"
    head_start, after = toks[0].start, toks[h.is_idx].end
    nxt = _at(toks, h.is_idx + 1)
    if _is(nxt, "d"):
        tag = nxt.text
        inner_start = nxt.start + len(tag)
        inner_end = max(inner_start, nxt.end - len(tag))
        lang = "plpgsql"
        rest = toks[h.is_idx + 2:]
        for k, t in enumerate(rest[:-1]):
            if _is(t, "w", "language") and rest[k + 1].kind in ("w", "q"):
                lang = rest[k + 1].text
                break
        return h.args, "\n" * text.count("\n", 0, inner_start) + text[inner_start:inner_end], lang
    head = text[head_start:after]
    return h.args, text[:head_start] + "DECLARE" + "\n" * head.count("\n") + text[after:], "plpgsql"


# ---------------------------------------------------------------- 包体:切出顶层子程序

@dataclass(frozen=True)
class Span:
    kind: str
    name: str
    start: int          # 过程头 PROCEDURE / FUNCTION 的起点(偏移)
    end: int            # 配对的 END [名字]; 之后


def _after_package_header(toks: List[_Tok]) -> Tuple[int, int]:
    """`[CREATE [OR REPLACE]] PACKAGE BODY 名 IS|AS` 之后的 (词元下标, 文本偏移);没有这个头就从头开始。"""
    for k, t in enumerate(toks[:12]):
        if _is(t, "w", "body"):
            for m in range(k + 1, min(len(toks), k + 6)):
                if _is(toks[m], "w", "is", "as"):
                    return m + 1, toks[m].end
            break
    return 0, 0


def _sub_end(toks: List[_Tok], i: int) -> Tuple[int, int]:
    """toks[i] 是子程序收尾的 END:跳过可选的名字与分号,返回 (下一个词元下标, 子程序结束偏移)。"""
    k = i + 1
    if _is(_at(toks, k), "w") or _is(_at(toks, k), "q"):
        k += 1
    end = toks[k].end if _is(_at(toks, k), "p", ";") else toks[k - 1].end
    return k + 1, end


def package_subprograms(text: str) -> Tuple[str, List[Span]]:
    """package body 全文 → (包级声明原文, 顶层子程序)。

    层次按 BEGIN / CASE 开、END 关来数(END IF、END LOOP 不计层;END CASE 关 CASE)。过程头只在第 0 层出现;
    还没开始过程体的过程头压栈 —— 声明区里的局部子程序也压栈,它们的 BEGIN … END 先配对出栈,不会被当成顶层。
    前置声明(过程头以分号结束)只算声明,不算子程序。字符串、注释、美元引号里的词一概不认。
    """
    toks = _tokens(text)
    i, decl_start = _after_package_header(toks)
    stack: List[List[Any]] = []          # [kind, name, start, body_started]
    spans: List[Span] = []
    depth, first_sub = 0, -1
    while i < len(toks):
        t = toks[i]
        if t.kind != "w":
            i += 1
            continue
        if t.text in _STANDALONE and depth == 0:
            h = _header(text, toks, i)
            if h is None:
                break
            if h.is_idx < 0:
                i = h.end_idx + 1
                continue
            if not stack and first_sub < 0:
                first_sub = t.start
            stack.append([h.kind, h.name, t.start, False])
            i = h.is_idx + 1
            continue
        if t.text == "begin":
            if depth == 0 and stack and not stack[-1][3]:
                stack[-1][3] = True
            depth += 1
        elif t.text == "case":
            depth += 1
        elif t.text == "end":
            nxt = _at(toks, i + 1)
            if _is(nxt, "w", "if", "loop"):
                i += 2
                continue
            depth -= 1
            if _is(nxt, "w", "case"):
                i += 1
            if depth < 0:
                break
            if depth == 0 and stack and stack[-1][3]:
                i, end = _sub_end(toks, i)
                kind, name, start, _ = stack.pop()
                if not stack:
                    spans.append(Span(kind, name, start, end))
                continue
        i += 1
    decls = text[decl_start:first_sub] if first_sub >= 0 else text[decl_start:]
    return decls, spans


__all__ = ["MAX_LINES", "Resolved", "Unavailable", "resolve", "fetch", "assemble", "normalize",
           "Span", "package_subprograms"]
