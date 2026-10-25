"""过程「找不到」时代为排查:连的库对不对、执行账号有没有权限、过程本身在不在,并列出要用户回答的问题。

现场(客户 2026-09-08 晚截图):procinfo 按三段名查 gmag.pckg_xxx.proc_maketext 得 0 行,脚本只报一句「找不到」;
模型守住了「不要解释成不支持包」的规矩,但手里没有任何可继续查的工具,写了两条跑不了的 SQL 后把问题推回用户
「确认拼写」。用户看到的是:过程明明在,skill 说没有。

og5 实测(A 兼容库,ALTER DATABASE … ENABLE PRIVATE OBJECT 对象隔离):
  · 隔离藏的是 pg_namespace 那一行——无权限账号查 pg_proc 单表仍有、查 gs_package 仍有,三表 JOIN 就成了 0 行;
  · 授 USAGE 后全部可见;pg_database 里没有任何标志能探出隔离开没开。
所以「过程在但 schema 不可见」可以推断出来(LEFT JOIN 的 nspname 为空),其余情况要用户或 DBA 回答。
GaussDB 505 是否连 pg_proc 也藏,没有真机不能断言——那种情况落到「本库没有」那一档,问题清单里给了 DBA 核对 SQL。

三条探测脚本(<skill>.locate_context / locate_schema / locate_search)每条独立 try:脚本没灌白名单就记「未能排查」
继续——排查本身绝不能再冒出一个报错。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from common.procname import ProcRef, qualified

CONTEXT = "locate_context"
SCHEMA = "locate_schema"
SEARCH = "locate_search"

_TRUE = {"t", "true", "1", "yes", "y", "on"}


@dataclass(frozen=True)
class Hit:
    kind: str        # "proc" | "package"
    schema: str      # "" = schema 对执行账号不可见(LEFT JOIN 的 nspname 为空)
    package: str
    name: str        # 过程名;包行为空


@dataclass(frozen=True)
class Locate:
    db: str = ""
    user: str = ""
    databases: Tuple[str, ...] = ()
    schema_seen: Optional[bool] = None     # None = 没探(没给 schema,或脚本没跑成)
    schema_usage: Optional[bool] = None
    hits: Tuple[Hit, ...] = ()
    skipped: Tuple[str, ...] = ()          # "项目:原因"


@dataclass(frozen=True)
class Verdict:
    code: str
    summary: str
    questions: Tuple[str, ...]
    actions: Tuple[str, ...]               # DBA 可照做的 SQL,逐条


# ---------------------------------------------------------------- probe

def _flag(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v or "").strip().lower() in _TRUE


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _first_line(exc: BaseException) -> str:
    """报错只留第一句:中间件「未注册脚本 X。已注册的有:…」那种会把全部脚本名带出来,用户看的是原因不是清单。"""
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    first = text.splitlines()[0]
    return first.split("。", 1)[0][:120]


def _run(runner, prefix: str, script: str, values: Dict[str, str],
         skipped: List[str], label: str) -> Optional[List[Dict[str, Any]]]:
    try:
        rows = runner.run("%s.%s" % (prefix, script), values)
    except Exception as exc:                  # noqa: BLE001 —— 排查本身不能再冒出一个报错
        skipped.append("%s:%s" % (label, _first_line(exc)))
        return None
    return [r for r in (rows or []) if isinstance(r, dict)]


def probe(runner, prefix: str, ref: ProcRef) -> Locate:
    """跑三条探测脚本,能跑几条算几条。"""
    skipped: List[str] = []
    db = user = ""
    databases: Tuple[str, ...] = ()
    rows = _run(runner, prefix, CONTEXT, {}, skipped, "当前连接")
    if rows:
        db, user = _s(rows[0].get("db")), _s(rows[0].get("usr"))
        databases = tuple(x.strip() for x in _s(rows[0].get("databases")).split(",") if x.strip())
    schema_seen: Optional[bool] = None
    schema_usage: Optional[bool] = None
    if ref.schema:
        rows = _run(runner, prefix, SCHEMA, {"schema": ref.schema}, skipped, "schema %s" % ref.schema)
        if rows is not None:
            schema_seen = bool(rows)
            schema_usage = _flag(rows[0].get("usage")) if rows else False
    hits: List[Hit] = []
    rows = _run(runner, prefix, SEARCH, {"name": ref.name, "package": ref.package}, skipped, "近似名搜索")
    for r in rows or []:
        hits.append(Hit(_s(r.get("kind")) or "proc", _s(r.get("nspname")), _s(r.get("pkgname")), _s(r.get("proname"))))
    return Locate(db, user, databases, schema_seen, schema_usage, tuple(hits), tuple(skipped))


# ---------------------------------------------------------------- classify

def _verify_sql(ref: ProcRef) -> str:
    return ("SELECT current_database() AS db, n.nspname, k.pkgname, p.proname\n"
            "FROM pg_proc p\n"
            "LEFT JOIN pg_namespace n ON n.oid = p.pronamespace\n"
            "LEFT JOIN gs_package k ON k.oid = p.propackageid\n"
            "WHERE p.proname ILIKE '%%%s%%';" % ref.name)


def _grant_usage(ref: ProcRef, acct: str) -> str:
    return "GRANT USAGE ON SCHEMA %s TO %s;" % (ref.schema or "<schema>", acct)


def _others(loc: Locate) -> str:
    others = [d for d in loc.databases if d and d != loc.db]
    return "、".join(others) if others else "(未取到库清单)"


def _hit_name(h: Hit) -> str:
    return qualified(h.schema or "(schema 不可见)", h.package, h.name)


def classify(ref: ProcRef, loc: Locate) -> Verdict:
    full = qualified(ref.schema, ref.package, ref.name)
    acct = loc.user or "<中间件执行账号>"
    db = loc.db or "<当前库>"
    verify = _verify_sql(ref)
    exact = [h for h in loc.hits if h.kind == "proc" and h.name == ref.name]
    hidden = [h for h in exact if not h.schema]
    if hidden:
        return Verdict(
            "schema_hidden",
            "过程 %s 在本库的 pg_proc 里有(%d 行同名),但它所在的 schema 对执行账号 %s 不可见:本库开启了对象隔离"
            "(ENABLE PRIVATE OBJECT)且该账号没有 schema %s 的 USAGE 权限,联表查询于是 0 行。这不是过程不存在。"
            % (ref.name, len(hidden), acct, ref.schema or "(未指定)"),
            ("请 DBA 按下面的命令给执行账号 %s 授权,授权后重跑本命令即可。" % acct,
             "如果 DBA 说本库没有开对象隔离,请把下面『DBA 核对』查询的结果发回,我们据此再查。"),
            tuple(a for a in (
                _grant_usage(ref, acct),
                ("GRANT EXECUTE ON PACKAGE %s.%s TO %s;" % (ref.schema, ref.package, acct)) if ref.package and ref.schema else "",
                verify) if a))
    if exact:
        cands = "、".join(sorted({_hit_name(h) for h in exact}))
        return Verdict(
            "elsewhere",
            "本库里叫 %s 的过程在:%s,不在你写的 %s。" % (ref.name, cands, full),
            ("你要看的是不是 %s 之一?是的话回复那个全名,按它重跑。" % cands,
             "如果都不是,过程可能建在别的库(实例上还有:%s)或名字有出入,请确认库名与全名。" % _others(loc)),
            (verify,))
    search_skipped = any(s.startswith("近似名搜索") for s in loc.skipped)
    if (ref.schema and loc.schema_seen is None) or search_skipped:
        return Verdict(
            "unknown",
            "排查脚本有 %d 项未能执行(见上面「未能排查」),无法判断是库不对、权限不够还是过程不存在。" % len(loc.skipped),
            ("请先按交付文档 08 把本次新增的白名单脚本(procinfo / proctune 的 locate_context、locate_schema、locate_search)"
             "灌入 script_config,再重跑本命令。",
             "或请 DBA 用管理员账号在库 %s 执行下面『DBA 核对』的查询并把结果发回。" % db),
            (verify,))
    if ref.schema and loc.schema_seen is False:
        return Verdict(
            "no_schema",
            "当前连的是库 %s(账号 %s),这个库里没有 schema %s——或者本库开了对象隔离且执行账号看不见它。"
            % (db, acct, ref.schema),
            ("过程 %s 是不是建在别的库里?当前连的是 %s,实例上还有:%s。是的话用 gaussdb-login 重新登录到那个库再跑。"
             % (full, db, _others(loc)),
             "如果确认就在库 %s,请 DBA 用管理员账号执行下面『DBA 核对』的查询并把结果发回:有结果就是执行账号 %s 的权限问题"
             "(按 GRANT 命令授权后重跑),没有结果就是这个库里确实没有。" % (db, acct)),
            (_grant_usage(ref, acct), verify))
    if ref.schema and loc.schema_usage is False:
        return Verdict(
            "no_usage",
            "schema %s 在本库,但执行账号 %s 没有它的 USAGE 权限;按名字没有查到 %s(权限不足时 GaussDB 可能把对象一并藏起来)。"
            % (ref.schema, acct, ref.name),
            ("请 DBA 先按下面的命令给执行账号 %s 授权,授权后重跑本命令。" % acct,
             "授权后仍找不到,请确认过程全名与所在库(当前 %s,实例上还有:%s)。" % (db, _others(loc))),
            (_grant_usage(ref, acct), verify))
    pkg_hits = [h for h in loc.hits if h.kind == "package"]
    near_procs = sorted({_hit_name(h) for h in loc.hits if h.kind == "proc" and h.name != ref.name})
    if ref.package and not any(h.package == ref.package for h in pkg_hits):
        near_pkgs = sorted({qualified(h.schema or "(schema 不可见)", h.package, "") for h in pkg_hits})
        return Verdict(
            "package_missing",
            "schema %s 在本库且执行账号可访问,但没有名为 %s 的包%s。"
            % (ref.schema, ref.package, ("(名字相近的包:%s)" % "、".join(near_pkgs)) if near_pkgs else ""),
            (("包名是否写对?名字相近的包有:%s,是其中之一就回复全名。" % "、".join(near_pkgs)) if near_pkgs
             else "包名是否写对?请把建包语句里的名字或慢 SQL 里 call 的原文发来。",
             "这个包是不是建在别的库?当前连的是 %s,实例上还有:%s。" % (db, _others(loc))),
            (verify,))
    if ref.package:
        summary = "schema %s 与包 %s 都在本库且执行账号可访问,但包里没有名为 %s 的过程。" % (ref.schema, ref.package, ref.name)
        list_sql = ("SELECT p.proname FROM pg_proc p WHERE p.propackageid = (SELECT k.oid FROM gs_package k "
                    "JOIN pg_namespace n ON n.oid = k.pkgnamespace WHERE n.nspname = '%s' AND k.pkgname = '%s');"
                    % (ref.schema, ref.package))
        actions: Tuple[str, ...] = (list_sql, verify)
    elif ref.schema:
        summary = "schema %s 在本库且执行账号可访问,但没有名为 %s 的过程/函数。" % (ref.schema, ref.name)
        actions = (verify,)
    else:
        summary = "本库(%s)里没有名为 %s 的过程/函数——已按名字在全部非系统 schema 里模糊搜过。" % (db, ref.name)
        actions = (verify,)
    return Verdict(
        "name_missing", summary,
        (("过程名是否写对?名字相近的有:%s,是其中之一就回复全名。" % "、".join(near_procs)) if near_procs
         else "过程名是否写对?请把慢 SQL 里 call 的原文或建过程语句里的名字发来(DBA 可用下面的查询列出候选)。",
         "过程是不是建在别的库?当前连的是 %s,实例上还有:%s。" % (db, _others(loc))),
        actions)


# ---------------------------------------------------------------- render

def _schema_line(ref: ProcRef, loc: Locate) -> str:
    if not ref.schema:
        return "- schema:未指定,按名字在全库搜"
    if loc.schema_seen is None:
        return "- schema %s:未探测(脚本未执行)" % ref.schema
    if loc.schema_seen is False:
        return "- schema %s:不在本库(或对象隔离下对执行账号不可见)" % ref.schema
    return "- schema %s:在,执行账号%s USAGE 权限" % (ref.schema, "有" if loc.schema_usage else "没有")


def _hits_line(ref: ProcRef, loc: Locate) -> str:
    if not loc.hits:
        return "- 近似名搜索:无(全部非系统 schema 里名字含 %s 的过程/包都没有)" % ref.name
    procs = ["过程 " + _hit_name(h) for h in loc.hits if h.kind == "proc"]
    pkgs = ["包 " + qualified(h.schema or "(schema 不可见)", h.package, "") for h in loc.hits if h.kind == "package"]
    return "- 近似名搜索:" + ";".join(x for x in ("、".join(procs), "、".join(pkgs)) if x)


def render(ref: ProcRef, tried: Sequence[str], loc: Locate, v: Verdict) -> str:
    full = qualified(ref.schema, ref.package, ref.name)
    ctx = ("库 %s,账号 %s;实例上的库:%s" % (loc.db, loc.user, "、".join(loc.databases) or "(未取到)")
           if loc.db else "(未取到)")
    tried_text = " / ".join(dict.fromkeys(tried))       # 两段名按 schema.proc 与 package.proc 各查一次,字面相同,只写一遍
    lines = ["# 过程未找到:%s" % full, "",
             "脚本按 %s 查过,当前库的 pg_proc 里没有这一行。已代为排查:" % tried_text, "",
             "- 当前连接:%s" % ctx, _schema_line(ref, loc), _hits_line(ref, loc)]
    if loc.skipped:
        lines.append("- 未能排查:" + ";".join(loc.skipped))
    lines += ["", "**结论**:%s" % v.summary, "", "请用户协助回答(按编号答复即可):"]
    lines += ["%d. %s" % (i, q) for i, q in enumerate(v.questions, 1)]
    if v.actions:
        lines += ["", "DBA 核对/处理(用管理员账号在库 %s 执行):" % (loc.db or "<当前库>"), "```sql"]
        lines += list(v.actions)
        lines.append("```")
    return "\n".join(lines) + "\n"


def to_json(ref: ProcRef, tried: Sequence[str], loc: Locate, v: Verdict) -> Dict[str, Any]:
    return {"not_found": {
        "proc": qualified(ref.schema, ref.package, ref.name),
        "tried": list(tried),
        "context": {"db": loc.db, "user": loc.user, "databases": list(loc.databases)},
        "schema": {"name": ref.schema, "seen": loc.schema_seen, "usage": loc.schema_usage},
        "hits": [asdict(h) for h in loc.hits],
        "skipped": list(loc.skipped),
        "verdict": v.code, "summary": v.summary,
        "questions": list(v.questions), "actions": list(v.actions),
    }}


def report(runner, prefix: str, exc, fmt: str = "markdown") -> str:
    """skill 收到 procname.NotFound 时调用:排查 + 判定 + 渲染,一次给全。"""
    loc = probe(runner, prefix, exc.ref)
    v = classify(exc.ref, loc)
    if fmt == "json":
        return json.dumps(to_json(exc.ref, exc.tried, loc, v), ensure_ascii=False, indent=2)
    return render(exc.ref, exc.tried, loc, v)


__all__ = ["CONTEXT", "SCHEMA", "SEARCH", "Hit", "Locate", "Verdict", "probe", "classify", "render", "to_json", "report"]
