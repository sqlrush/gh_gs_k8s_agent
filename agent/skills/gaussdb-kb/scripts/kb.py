#!/usr/bin/env python3
"""gaussdb-kb(查询):query / health / search / cite-check。

导入、索引、审批在 gaussdb-kb-import;runtime 镜像里没有那些代码。
本文件只是命令入口,检索逻辑在 common/kb/。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))           # 兄弟模块 kb_cite
for _anc in _HERE.parents:                       # common/(仓库根,或装好后的 skills/ 下)
    if (_anc / "common" / "kb" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import kb_cite  # noqa: E402  —— 在 sys.path 刚放好时就绑定,调用方之后怎么改 sys.path 都不影响
import kb_read  # noqa: E402  —— 分层读取:overview / topic / show / evidence / template
from common import reports  # noqa: E402
from common.kb import catalog as kbcat  # noqa: E402
from common.kb import config as kbconfig  # noqa: E402
from common.kb import indexer, query as kbquery, render  # noqa: E402
from common.kb.rulesfile import SEARCH_HIT_CAP, SEARCHABLE, iter_files, read_text_file  # noqa: E402


class KbError(Exception):
    """Operator-facing failure; message is printed as-is."""


# 导入侧的子命令在本环境(runtime 镜像)物理不存在。保留命令名做桩:模型按记忆调用时得到「去哪、找谁」,
# 而不是 argparse 的 invalid choice——那句话对模型没有指导意义,它会开始猜别的命令或自己动手写目录。
IMPORT_ONLY = ("ingest", "index", "validate", "propose", "review", "apply", "setup", "feedback", "eval", "contract",
               "add", "attach", "retype", "migrate", "profile", "taxonomy", "inbox-clean", "docset")
IMPORT_ONLY_MSG = ("本环境不含知识库导入功能(gaussdb-kb-import),请联系知识库管理员在导入环境操作。"
                   "本环境可用:query / health / search / cite-check / overview / topic / doc / show / evidence。")


def cmd_import_only(args: argparse.Namespace) -> int:
    print("[error] %s" % IMPORT_ONLY_MSG, file=sys.stderr)
    return 2


# ---------------------------------------------------------------- search

def _grep_file(path: pathlib.Path, kb: pathlib.Path, needle: str,
               prefix: str = "") -> list[str]:
    """Literal, case-insensitive line matches — the same thing the skills' grep does."""
    try:
        content = read_text_file(path)
    except OSError as exc:
        print(f"{path.relative_to(kb)}: 读取失败:{exc}", file=sys.stderr)
        return []
    return [f"{prefix}{path.relative_to(kb)}:{lineno}: {line.strip()}"
            for lineno, line in enumerate(content.splitlines(), 1)
            if needle in line.lower()]


def cmd_search(args: argparse.Namespace) -> int:
    kb = kbconfig.resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise KbError(f"KB 目录不存在:{kb}")
    needle = args.keyword.lower()

    # Mirrors the skills' grep range exactly — archive/ is not in it.
    hits: list[str] = []
    for sub, suffixes in SEARCHABLE:
        for path in iter_files(kb, sub, suffixes):
            hits += _grep_file(path, kb, needle)

    archived = [h for p in iter_files(kb, "archive", (".yaml", ".yml"))
                for h in _grep_file(p, kb, needle, prefix="[已废止] ")]

    with_archive = bool(getattr(args, "include_archived", False))
    shown = hits + (archived if with_archive else [])
    for line in shown[:SEARCH_HIT_CAP]:
        print(line)
    if len(shown) > SEARCH_HIT_CAP:
        print(f"(命中超过 {SEARCH_HIT_CAP} 条,已截断——换更具体的关键词)")

    if hits:
        return 0

    # Nothing *current* matched. Which line to print depends on whether we just
    # listed withdrawn ones: saying 「未命中」 directly under a list of hits is a
    # self-contradiction, and 「未命中」 is the line carrying the discipline (never
    # pass your own knowledge off as the customer's spec), so it must land on the
    # right case rather than being sprayed at both.
    miss = (f"未命中:'{args.keyword}'(KB={kb})。"
            "知识库未覆盖时必须如实说明,不得用自带知识冒充规范。")

    if with_archive and archived:
        print(f"现行条款未命中:'{args.keyword}' —— 上列 {len(archived)} 行均为"
              "**已废止**条款,仅供追溯,不得用于判定。")
    elif archived:
        # The re-import trap: without this note the model concludes 「知识库没这条」
        # while a withdrawn clause on exactly that topic sits in archive/. Say that it
        # exists; never print its text, or it becomes usable for judging.
        print(miss)
        print(f"注:archive/ 中另有 {len(archived)} 行**已废止**条款命中该关键词"
              "(不得用于判定;确需查阅历史加 --include-archived)。")
    else:
        print(miss)
    return 0


# ---------------------------------------------------------------- query

def _load_findings(path: pathlib.Path):
    from common.finding import findings_from_json
    return findings_from_json(path.read_text(encoding="utf-8"))


def _how(status) -> str:
    """大盘要让用户知道自己看到的是语义命中还是关键词命中:文件模式下只有关键词。"""
    semantic = bool(status.mode) and status.mode != kbquery.MODE_FILES and "超时" not in (status.vector or "")
    return "semantic" if semantic else "keyword"


def _log_queries(result) -> None:
    """每个检索项追加一行到本人 reports/kb/queries.jsonl —— 知识库大盘「最近检索」的来源。
    没设 GSDB_REPORTS_DIR 时 append_jsonl 直接返回,行为与现在一样。"""
    import datetime as _dt
    at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for it in result.items:
        reports.append_jsonl("kb", "queries", {
            "at": at, "q": it.query, "key": it.key,
            "hits_cases": len(it.cases), "hits_rules": len(it.clauses),
            "how": _how(result.status), "elapsed_ms": result.elapsed_ms})


def cmd_query(args: argparse.Namespace) -> int:
    kb = kbconfig.resolve_kb_dir(args.kb)
    if args.from_findings:
        findings = _load_findings(pathlib.Path(args.from_findings))
        result = kbquery.from_findings(findings, kb_dir=kb, alias=getattr(args, "alias", "") or "")
    else:
        result = kbquery.from_text(args.q, kb_dir=kb, alias=getattr(args, "alias", "") or "")
    if args.json:
        print(json.dumps(kbquery.result_to_dict(result), ensure_ascii=False, indent=2))
    else:
        print(render.render_section(result), end="")
    _log_queries(result)
    return 0 if result.status.attached else 2


# ---------------------------------------------------------------- health

def _misses_top(kb: pathlib.Path, n: int = 10) -> list[tuple[str, int]]:
    path = kb / "index" / "misses.log"
    if not path.is_file():
        return []
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            counts[parts[1]] = counts.get(parts[1], 0) + 1
    return sorted(counts.items(), key=lambda p: (-p[1], p[0]))[:n]


def _pending(kb: pathlib.Path) -> list[str]:
    out = []
    inbox = kb / "inbox"
    if not inbox.is_dir():
        return out
    for slug_dir in sorted(p for p in inbox.iterdir() if p.is_dir()):
        items = list((slug_dir / "items").glob("*.md")) if (slug_dir / "items").is_dir() else []
        cand = slug_dir / "candidates.json"
        dec = slug_dir / "decisions.yaml"
        if items and not cand.exists():
            out.append(f"inbox/{slug_dir.name}: {len(items)} 单待 propose")
        elif cand.exists() and not dec.exists():
            out.append(f"inbox/{slug_dir.name}: 候选待 review/确认")
        elif dec.exists():
            out.append(f"inbox/{slug_dir.name}: 有 decisions 待 apply")
        elif (slug_dir / "source.md").exists():
            out.append(f"inbox/{slug_dir.name}: 规范待条款化")
    return out


def health_dict(kb: pathlib.Path, status, file_warnings: list, readonly: bool) -> dict:
    """大盘知识库页的数据形状。文本输出与它同源,一份定义。"""
    from common.kb import inbox as kbinbox
    return {
        "status": dict(status.__dict__),
        "readonly": readonly,
        "inbox": str(kbinbox.inbox_dir(kb)),
        "file_warnings": list(file_warnings),
        "index_state": indexer.read_state(kb) or {},
        "pending": _pending(kb),
        "misses": [{"code": code, "n": n} for code, n in _misses_top(kb)],
        "types": kbcat.coverage(kb),
        "docsets": _docsets_summary(kb),
    }


def _docsets_summary(kb: pathlib.Path) -> list:
    from common.kb import docset as kbdocset
    out = []
    for ds in kbdocset.installed(kb):
        m, idx = ds.manifest, ds.manifest.get("index") or {}
        out.append({"code": ds.code, "title": m.get("title", ""), "product": m.get("product", ""),
                    "versions": str(m.get("versions", "")), "items": m.get("items", 0),
                    "model_summaries": m.get("model_summaries", 0), "index_complete": bool(idx.get("complete")),
                    "docs_left": idx.get("docs_left", 0)})
    return out


def cmd_health(args: argparse.Namespace) -> int:
    kb = kbconfig.resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise KbError(f"KB 目录不存在:{kb}")
    sess = kbquery.KbSession.open(kb)
    try:
        status = sess.status()
        file_warnings = list(getattr(sess.pg, "warnings", ()))[:5]    # 文件模式:坏文件在这里露头
    finally:
        sess.close()
    readonly = not os.access(kb, os.W_OK)
    d = health_dict(kb, status, file_warnings, readonly)
    if getattr(args, "json", False):
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(render.status_line(status))
        if readonly:
            print("知识库只读  : 是(本环境不能导入;由知识库管理员在导入环境维护)")
        print(f"收件目录  : {d['inbox']}(用户要导入自己电脑上的文件时,先上传到这里再 ingest)")
        try:
            legacy = kbconfig.load(kb).store.legacy_pg
        except kbconfig.KbConfigError:
            legacy = False
        if legacy:
            print("[warn ] kb.yaml 里的 store.pg 已不再使用(2026-10-23 起向量库改为知识库目录里的 index/store.sqlite,"
                  "不用配),由知识库管理员删掉这一段即可")
        for w in file_warnings:
            print(f"[warn ] 文件:{w}")
        state = d["index_state"]
        if state:
            print(f"上次索引  : {state.get('indexed_at', '?')} · 文档新写 {state.get('docs_indexed', '?')} · "
                  f"覆盖 {state.get('chunk_embedded', '?')}/{state.get('chunk_total', '?')} · 图 {state.get('graph', '?')}")
        if d.get("types"):
            print("知识类型  : " + " · ".join(f"{name} {d['types'].get(k, 0)}" for k, name in kbcat.COVERAGE_ROWS
                                              if k != "guide" or d["types"].get(k)))
            print(f"下一步    : {'(请知识库管理员)' if readonly else ''}{kbcat.next_step(d['types'])}")
        for ds in d["docsets"]:
            state = "完整" if ds["index_complete"] else "未完成(还差 %s 条,导入环境跑 kb.py index)" % ds["docs_left"]
            print(f"官方文档集: {ds['code']} {ds['title']} · {ds['product']} {ds['versions']} · {ds['items']} 条 · 索引{state}")
        try:
            if d["docsets"] and kbconfig.load(kb).docset_applicability_unknown:
                print("[warn ] 测试开关开着:官方文档集对所有实例按「适用性未知」处理——只许在测试环境;正式环境关掉"
                      "(kb.yaml test_switches.docset_applicability_unknown)")
        except kbconfig.KbConfigError:
            pass
        print("待处理    : " + ("; ".join(d["pending"]) if d["pending"] else "无"))
        if d["misses"]:
            print("缺口清单  : 近期查不到条款/案例的发现 Top —— " +
                  "、".join(f"{m['code']}×{m['n']}" for m in d["misses"]) + "(补这类材料收益最大)")
        else:
            print("缺口清单  : 无记录")
        if not status.attached:
            print(f"[error] 知识库未接入:{status.reason}")
    # 每次覆盖 kb/health.json:大盘知识库页读它;失败只 warn,不改退出码
    reports.announce("kb", reports.archive("kb", d, name="health"), "")   # 标准错误:大盘知识库页的直达链接
    if not status.attached:
        return 2
    return 2 if d["pending"] else 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kb.py",
        description="GaussDB 客户知识库(查询):检索 · 分层读取(L0 overview / L1 topic / L2 show / L3 evidence)· 大盘 · 引用核对。"
                    "导入与索引在 gaussdb-kb-import。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("query", help="检索:--q 自然语言,或 --from-findings findings.json")
    p.add_argument("--kb")
    p.add_argument("--q", help="问题文本")
    p.add_argument("--from-findings", help="skill 输出的 findings json 文件")
    p.add_argument("--alias", help="当前连接别名(如 og5):按它匹配环境画像,判断条目适不适用本实例")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("health", help="文本大盘:接入状态、条款/案例/边数、覆盖率、待处理、缺口清单")
    p.add_argument("--kb")
    p.add_argument("--json", action="store_true", help="按大盘的数据形状输出 JSON")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("search", help="检索知识库(errata 优先;不含已废止条款)")
    p.add_argument("keyword")
    p.add_argument("--kb")
    p.add_argument("--include-archived", action="store_true",
                   help="连 archive/ 里的已废止条款一并列出(仅供人工追溯,不得用于判定)")
    p.set_defaults(func=cmd_search)

    kb_cite.add_subcommands(sub)
    kb_read.add_subcommands(sub)

    for name in IMPORT_ONLY:
        p = sub.add_parser(name, help="(本环境不可用,见 gaussdb-kb-import)")
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(func=cmd_import_only)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in IMPORT_ONLY:          # 不解析参数:导入命令的参数形状本环境不认识,也不需要认识
        return cmd_import_only(argparse.Namespace())
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (KbError, kb_cite.CiteCmdError, kb_read.ReadCmdError, kbconfig.KbConfigError) as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
