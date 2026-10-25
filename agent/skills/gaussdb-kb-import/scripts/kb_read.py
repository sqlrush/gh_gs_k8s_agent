"""kb 只读命令:渐进式披露的四层读取入口(诊断环境与导入环境都有)。

    overview                 L0 总索引:知识范围(八类各多少)、适用环境(画像)、专题入口
    topic [<专题>] [--all]   L1 专题导航:同义词、本行确认过的候选原因与区分证据、相关条目摘要
    topic --list             专题清单(slug、名称、条目数)
    show <ID> [--brief]      L2 一条条目的全文(--brief 只到摘要);一次只取一条
                             官方文档的长条目先给摘要与小节清单,--section / --grep 再展开,--full 全文
    evidence <ID>            L3 这条条目的原始证据:原文片段、照片 / 日志的位置(官方文档:原页)
    doc [<代号> [章节]]      L1 官方文档集的目录导航(按原书目录下钻;--topic 只看某专题)
    template <类型>          新条目的骨架(frontmatter + 小节),导入时照着写

每条命令的输出都有上限(L0 3000 字、L1 6000 字、show / evidence 8000 字),超出写明截断。
文件是真相:每次现读,知识库目录只读也能用,不依赖 index 产物新不新。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Any, List, Optional, Tuple

import yaml

from common.kb import catalog as kbcat
from common.kb import config as kbconfig
from common.kb import docindex as kbdocindex
from common.kb import docnav as kbdocnav
from common.kb import docset as kbdocset
from common.kb import docsplit as kbdocsplit
from common.kb import graphfiles as gf
from common.kb import layers as kblayers
from common.kb import topics as kbtopics
from common.kb import types as kbtypes
from common.kb import versions as kbversions

SHOW_LIMIT = 8000
EVIDENCE_LIMIT = 8000
DOC_FULL_UNDER = 3000        # 官方文档条目正文不超过这么多字就直接给全文,超过先给摘要与小节清单
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff")
_TEXT_SUFFIXES = (".md", ".txt", ".log", ".csv", ".yaml", ".yml", ".json", ".sql")


class ReadCmdError(Exception):
    """用户可读的错误(目录不存在、ID 不存在)。"""


def _kb(args: argparse.Namespace) -> pathlib.Path:
    kb = kbconfig.resolve_kb_dir(getattr(args, "kb", None))
    if not kb.is_dir():
        raise ReadCmdError(f"知识库目录不存在:{kb}")
    return kb


def _version(kb: pathlib.Path) -> str:
    try:
        return (kb / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _cap(text: str, limit: int, hint: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + f"\n(超出 {limit} 字已截断:{hint})\n"


# ---------------------------------------------------------------- overview / topic

def cmd_overview(args: argparse.Namespace) -> int:
    kb = _kb(args)
    cat = kbcat.load(kb)
    print(kblayers.render_l0(kb, cat, _version(kb)), end="")
    if args.alias:
        prof = kbcat.profile_for_alias(cat, args.alias)
        print(f"\n本实例({args.alias}):" + (f"{prof.id}《{prof.title}》" if prof else "未登记环境画像(适用性按「未知」处理)"))
    for w in cat.warnings:
        print(f"[warn ] {w}")
    return 0


def cmd_topic(args: argparse.Namespace) -> int:
    kb = _kb(args)
    cat = kbcat.load(kb)
    if args.list or not args.slug:
        print("| 专题 | slug | 条目 |\n|---|---|---|")
        for t in list(cat.topics) + [kbtopics.Topic(kbtopics.OTHER, kbtopics.OTHER_NAME)]:
            n = len(cat.in_topic(t.slug))
            if n or t.slug != kbtopics.OTHER:
                print(f"| {t.name} | `{t.slug}` | {n} |")
        print("\n用法:kb.py topic <slug>(看某个专题的候选原因与相关条目)")
        return 0
    slugs = {t.slug for t in cat.topics} | {kbtopics.OTHER}
    slug = args.slug.strip()
    if slug not in slugs:
        by_name = [t.slug for t in cat.topics if t.name == slug or slug in t.synonyms]
        if not by_name:
            raise ReadCmdError(f"没有专题 `{slug}`;可用:{'、'.join(sorted(slugs))}(kb.py topic --list)")
        slug = by_name[0]
    triples, _ = gf.load_triples(kb, case_ids=[e.id for e in cat.of_type(kbtypes.CASE)])
    view = kblayers.topic_view(cat, triples, slug)
    print(kblayers.render_l1(view, _version(kb), show_all=args.all), end="")
    return 0


# ---------------------------------------------------------------- show

def _suggest(cat: kbcat.Catalog, key: str) -> List[str]:
    k = key.lower()
    return [e.id for e in cat.entries if k in e.id.lower()][:8]


def _clause_text(e: kbcat.Entry) -> str:
    return yaml.safe_dump([{k: v for k, v in e.meta.items()}], allow_unicode=True, sort_keys=False)


def _brief_md(text: str, entry: kbcat.Entry) -> str:
    """frontmatter + 摘要(案例没有摘要节,给根因与处置的一句话)。"""
    m = re.match(r"^---\s*\n.*?\n---\s*\n?", text, re.S)
    front = m.group(0) if m else ""
    return f"{front}\n## 摘要\n{entry.summary}\n"


def cmd_show(args: argparse.Namespace) -> int:
    kb = _kb(args)
    cat = kbcat.load(kb)
    e = cat.find(args.id)
    if e is None:
        near = _suggest(cat, args.id)
        raise ReadCmdError(f"知识库里没有 `{args.id}`" + (f";相近的:{'、'.join(near)}" if near else "")
                           + "——没有就是没有,不得编造(回答里引用了的 ID 先跑 cite-check)")
    head = [f"# {e.cite} {e.title}",
            f"> 类型:{kbtypes.TYPE_NAMES.get(e.type, (e.type,))[0]} · 文件:`{e.file}`"
            + (f" · 专题:{'、'.join(e.topics)}" if e.topics else "")
            + (f" · {e.verification}" if e.verification else "")]
    if e.id != args.id.strip():
        head.append(f"> `{args.id}` 是旧 ID,已改为 `{e.id}`(引用写新 ID)")
    if e.archived:
        head.append("> ⚠ **已废止条款**,仅供追溯,不得用于判定")
    if e.meta.get("docset"):
        return _show_docset(kb, e, args, head)
    if args.alias and e.type not in (kbtypes.CASE, "profile"):
        prof = kbcat.profile_for_alias(cat, args.alias)
        if e.type == "defect":
            head.append(f"> 适用性:{kbversions.defect_verdict(e.meta, prof.meta if prof else None)}")
        else:
            v = kbversions.judge(e.meta, prof.meta if prof else None)
            head.append(f"> 适用性:{v.state}" + (f"({v.reason})" if v.reason else ""))
    if e.type in (kbtypes.NORM,):
        body = _clause_text(e)
    else:
        try:
            body = (kb / e.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ReadCmdError(f"{e.file} 读不了:{exc}")
        if args.brief:
            body = _brief_md(body, e)
    n_ev = len(e.evidence) + (1 if e.source else 0)
    foot = f"\n(原始证据 {n_ev} 处:kb.py evidence {e.id})\n" if n_ev else ""
    print(_cap("\n".join(head) + "\n\n" + body.rstrip() + "\n" + foot, SHOW_LIMIT, f"kb.py evidence {e.id} 看原文"), end="")
    return 0


def _docset_applies(kb: pathlib.Path, e: kbcat.Entry, alias: str) -> str:
    try:
        cfg = kbconfig.load(kb)
    except kbconfig.KbConfigError:
        cfg = None
    if cfg is not None and cfg.docset_applicability_unknown:
        return "未知(测试开关:官方文档适用性按未知处理)"
    if not alias:
        return ""
    prof = kbcat.profile_for_alias(kbcat.load(kb, with_docsets=False), alias)
    v = kbversions.judge(e.meta, prof.meta if prof else None)
    return v.state + (f"({v.reason})" if v.reason else "")


def _show_docset(kb: pathlib.Path, e: kbcat.Entry, args: argparse.Namespace, head: List[str]) -> int:
    """官方文档条目:短的给全文;长的先给摘要与小节清单(p99 一页 3.7 万字,整页进上下文会挤掉推理空间)。"""
    path = kb / e.file
    try:
        meta, secs = kbdocindex.parse_item(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReadCmdError(f"{e.file} 读不了:{exc}")
    head[1] += f" · 文档集 {e.meta.get('docset')} · {e.meta.get('product', '')} {e.meta.get('versions', '')}"
    head.append(f"> 目录:{' › '.join(meta.get('doc_path') or [])}")
    applies = _docset_applies(kb, e, getattr(args, "alias", "") or "")
    if applies:
        head.append(f"> 适用性:{applies}")
    if meta.get("summary_by") == "model":
        head.append("> 摘要由模型依据原页撰写(脚本已核对其中的参数名、数值都出自原页);细节以下面原文为准")
    body_secs = {k: v for k, v in secs.items() if k != "摘要"}
    total = sum(len(v) for v in body_secs.values())
    out = head + ["", "## 摘要", secs.get("摘要", "")]
    if getattr(args, "grep", None):
        if meta.get("kind") == kbdocsplit.KIND_ERRCODE:
            found = kbdocsplit.find_errcode("\n".join(body_secs.values()), args.grep)
        else:
            found = [ln for v in body_secs.values() for ln in v.splitlines() if args.grep.lower() in ln.lower()]
        out += ["", f"## 含「{args.grep}」的内容({len(found)} 处)"] + (found[:30] or ["(没找到)"])
    elif getattr(args, "section", None):
        name = next((k for k in body_secs if k == args.section), None) or \
            next((k for k in body_secs if args.section in k), None)
        if name is None:
            raise ReadCmdError(f"{e.id} 没有小节「{args.section}」;有:{'、'.join(body_secs)}")
        out += ["", f"## {name}", body_secs[name]]
    elif args.brief:
        pass
    elif getattr(args, "full", False) or total <= DOC_FULL_UNDER:
        for k, v in body_secs.items():
            out += ["", f"## {k}", v]
    else:
        out += ["", f"## 小节(全文 {total} 字,按需展开)"]
        out += [f"- {k}({len(v)} 字):`kb.py show {e.id} --section \"{k}\"`" for k, v in body_secs.items()]
        hint = "--grep <错误码号或报错原文>" if meta.get("kind") == kbdocsplit.KIND_ERRCODE else "--grep <关键词>"
        out += ["", f"(只看相关行:`kb.py show {e.id} {hint}`;确需全文:`--full`)"]
    out += ["", f"(原页:kb.py evidence {e.id})"]
    print(_cap("\n".join(out) + "\n", SHOW_LIMIT, f"用 --section 分节看,或 kb.py evidence {e.id}"), end="")
    return 0


def cmd_doc(args: argparse.Namespace) -> int:
    kb = _kb(args)
    sets = kbdocset.installed(kb)
    if not args.code:
        if not sets:
            print("(没有装官方文档集)")
            return 0
        print("| 代号 | 文档 | 产品与版本 | 条目 |\n|---|---|---|---|")
        for ds in sets:
            m = ds.manifest
            print(f"| {ds.code} | {m.get('title', '')} | {m.get('product', '')} {m.get('versions', '')} | {m.get('items', 0)} |")
        print("\n用法:kb.py doc <代号> [章节] [--topic <专题>]")
        return 0
    if args.code not in {d.code for d in sets}:
        raise ReadCmdError(f"没有文档集 {args.code}(已装:{'、'.join(d.code for d in sets) or '无'})")
    cat = kbcat.load(kb)
    print(kbdocnav.render_nav(cat, args.code, chapter=args.chapter or "", topic=args.topic or "",
                              show_all=args.all), end="")
    return 0


# ---------------------------------------------------------------- evidence

def _split_ref(ref: Any) -> Tuple[str, str, str]:
    """(路径, 定位, 引句)。ref 可以是 'sources/x.docx#第3节' 或 {file, at, quote}。"""
    if isinstance(ref, dict):
        return str(ref.get("file") or ""), str(ref.get("at") or ""), str(ref.get("quote") or "")
    s = str(ref or "")
    path, _, loc = s.partition("#")
    return path.strip(), loc.strip(), ""


def _resolve(kb: pathlib.Path, rel: str) -> Optional[pathlib.Path]:
    if not rel:
        return None
    cands = [kb / rel] + ([kb / "sources" / rel, kb / "evidence" / rel, kb / "inbox" / "uploads" / rel] if "/" not in rel else [])
    for c in cands:
        try:
            c.resolve().relative_to(kb.resolve())        # 不许 ../ 跑出知识库目录
        except ValueError:
            continue
        if c.is_file():
            return c
    return None


def _text_of(path: pathlib.Path) -> Optional[str]:
    suffix = path.suffix.lower()
    try:
        if suffix in _TEXT_SUFFIXES:
            return path.read_text(encoding="utf-8", errors="replace")
        from common.kb import extract
        return extract.extract_source(path)
    except Exception:                                    # noqa: BLE001  抽不出文字就只给路径
        return None


def _excerpt(text: str, loc: str, quote: str) -> str:
    lines = text.splitlines()
    if quote:
        i = text.find(quote.strip()[:40])
        if i >= 0:
            return text[max(0, i - 300):i + len(quote) + 300].strip()
    m = re.fullmatch(r"(?:item=)(\d+)", loc)
    if m:
        blocks = [b for b in re.split(r"\n-{3,}\n", text) if b.strip()]
        k = int(m.group(1))
        return blocks[k - 1].strip() if 0 < k <= len(blocks) else ""
    m = re.fullmatch(r"L?(\d+)(?:-L?(\d+))?", loc)
    if m:
        a = int(m.group(1)); b = int(m.group(2) or a + 20)
        return "\n".join(lines[a - 1:b])
    if loc:
        for i, ln in enumerate(lines):
            if loc in ln:
                return "\n".join(lines[i:i + 25])
    return "\n".join(lines[:40])


def cmd_evidence(args: argparse.Namespace) -> int:
    kb = _kb(args)
    cat = kbcat.load(kb)
    e = cat.find(args.id)
    if e is None:
        raise ReadCmdError(f"知识库里没有 `{args.id}`")
    if e.meta.get("docset"):
        return _evidence_docset(kb, e)
    refs = list(e.evidence) + ([e.source] if e.source and e.source not in e.evidence else [])
    out = [f"# {e.cite} 的原始证据(L3)", ""]
    if not refs:
        out.append("(这条没有登记出处或证据;只能按条目本身引用,并说明「未附原始证据」)")
    for ref in refs:
        rel, loc, quote = _split_ref(ref)
        path = _resolve(kb, rel)
        label = f"`{rel}`" + (f" · 定位:{loc}" if loc else "")
        if path is None:
            out += [f"## {label}", "⚠ 知识库里找不到这个文件(原件没存档,或是外部出处),只能按条目引用", ""]
            continue
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            out += [f"## {label}", f"图片:{path}(需要核对时用 read 工具查看这张图)", ""]
            continue
        text = _text_of(path)
        if text is None:
            out += [f"## {label}", f"文件:{path}(抽不出文字;docx/pdf 以外的格式请让知识库管理员转成文本)", ""]
            continue
        snippet = _excerpt(text, loc, quote)
        out += [f"## {label}", "```text", snippet[:2500] or "(定位没找到,下面是开头)\n" + text[:1500], "```", ""]
    print(_cap("\n".join(out) + "\n", EVIDENCE_LIMIT, "证据太长,按定位逐条看"), end="")
    return 0


def _evidence_docset(kb: pathlib.Path, e: kbcat.Entry) -> int:
    meta, _secs = kbdocindex.parse_item((kb / e.file).read_text(encoding="utf-8"))
    page = kb / kbdocset.DOCSETS_DIR / str(e.meta.get("docset")) / str(meta.get("page") or "")
    out = [f"# {e.cite} 的原始证据(L3)", "", f"出处:{meta.get('source', '')}"]
    if not page.is_file():
        out.append("⚠ 原页不在知识库里(文档集不完整),只能按条目引用")
    else:
        text = page.read_text(encoding="utf-8")
        name = str(meta.get("doc_key") or "").partition("#")[2]
        if name and f"### {name}" in text:                 # GUC 参数:给本参数那一段
            i = text.index(f"### {name}")
            nxt = text.find("\n### ", i + 4)
            text = text[i:nxt if nxt > 0 else len(text)]
            out.append(f"原页:{page.relative_to(kb)}(以下是本参数一段;同页其余参数 `kb.py show` 对应条目)")
        else:
            out.append(f"原页:{page.relative_to(kb)}")
        out += ["", "```text", text[:6000], "```"]
    print(_cap("\n".join(out) + "\n", EVIDENCE_LIMIT, "原页太长,用 kb.py show <ID> --grep 定位"), end="")
    return 0


# ---------------------------------------------------------------- template

_TEMPLATE_META = {
    "runbook": {"applies": {"product": "GaussDB 集中式", "versions": ">= 503", "systems": ["*"]},
                "not_applies": "<什么情况下不能用>", "verification": "未验证", "approval": ["<要的审批 / 规范 ID>"]},
    "defect": {"affected_versions": "<受影响版本,如 505.1–505.2>", "fixed_in": "<修复版本 / 补丁>",
               "vendor_status": "待确认"},
    "baseline": {"system": "<系统>", "metric": "<指标名,如 active_sessions>", "unit": "<单位>",
                 "metric_source": "<哪个技能 / 视图>", "window": "<采样窗口>", "value_kind": "差值"},
    "product": {"product": "GaussDB 集中式", "versions": "<版本范围,如 505.*>", "deployment": "<部署形态>"},
    "wiki": {"verification": "未验证", "as_of": "<YYYY-MM>"},
    "profile": {"system": "<系统简称>", "aliases": ["<连接别名,如 og5>"], "product": "GaussDB 集中式",
                "version": "<如 505.2.0>", "deployment": "<如 一主两备>"},
}


def template_text(type_code: str) -> str:
    kt = kbtypes.TYPED[type_code]
    meta = {"id": "auto", "type": kt.code, "title": "<一句话标题>"}
    if kt.needs_topics:
        meta["topics"] = ["<专题 slug,如 lock>"]
    if kt.needs_source:
        meta["source"] = "<出处:sources/原文.docx#章节>"
    meta.update(_TEMPLATE_META.get(type_code, {}))
    meta["evidence"] = ["<可选:sources/... 或 evidence/... 的原文、照片、日志>"]
    sections = ["## 摘要", "<≤200 字,结论在前;L1 专题导航里只显示这一段>", ""]
    for s in kt.recommended_sections:
        sections += [f"## {s}", "<…>", ""]
    if type_code != "profile":
        sections += ["## 判断边界", "<什么情况下不适用>", ""]
    return ("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip() + "\n---\n"
            + "\n".join(sections))


def schema_lines(type_code: str) -> list:
    """这一类的必填 / 建议 / 取值范围。2026-10-24 测试环境:模型为了知道 value_kind 能填什么去翻脚本源码
    (还用了不放行的 sed,界面显示「shell 失败」)——字段要求直接在 template 里说全。"""
    kt = kbtypes.TYPED[type_code]
    required = ["id(写 auto)", "type", "title"] + (["topics"] if kt.needs_topics else []) \
        + (["source"] if kt.needs_source else []) + list(kt.required_meta) + ["## 摘要"]
    if type_code == "product":
        required = [r if r != "product" else "product(产品系列)" for r in required]
    lines = [f"必填:{'、'.join(required)}"]
    rec = list(kt.recommended_meta) + [f"## {s}" for s in kt.recommended_sections]
    if rec:
        lines.append(f"建议:{'、'.join(rec)}(缺了只告警)")
    for key, allowed in kt.enums:
        lines.append(f"取值:{key} 只能是 {' / '.join(allowed)}")
    lines.append("可选:applies {product, versions, systems, deployment}、not_applies、evidence、synonyms;"
                 "版本范围写法 505.2 / 505.* / >= 503 / 505.1–505.2 / *")
    if type_code == "runbook":
        lines.append("手册 ID 的域:写 domain(2–4 个大写字母,如 LCK),不写就按第一个专题取")
    if type_code == "profile":
        lines.append("画像只写连接别名(aliases),不写 IP / 主机名:端口 / 口令(校验会拦)")
    return lines


def cmd_template(args: argparse.Namespace) -> int:
    kt = kbtypes.TYPED[args.type]
    print(f"# {kt.name}({args.type},放 {kt.dir}/,ID {kt.id_hint};id 写 auto 由 kb.py add 分配)")
    for line in schema_lines(args.type):
        print(f"#   {line}")
    print()
    print(template_text(args.type), end="")
    return 0


# ---------------------------------------------------------------- wiring

def add_subcommands(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("overview", help="L0 总索引:知识范围、适用环境、专题入口")
    p.add_argument("--kb")
    p.add_argument("--alias", help="当前连接别名:顺带说本实例匹配到哪份环境画像")
    p.set_defaults(func=cmd_overview)
    p = sub.add_parser("topic", help="L1 专题导航:候选原因与区分证据、相关条目摘要")
    p.add_argument("slug", nargs="?")
    p.add_argument("--kb")
    p.add_argument("--all", action="store_true", help="列出本专题全部条目(默认按上限截断)")
    p.add_argument("--list", action="store_true", help="只列专题清单")
    p.set_defaults(func=cmd_topic)
    p = sub.add_parser("show", help="L2 一条条目的全文(引用前先读)")
    p.add_argument("id")
    p.add_argument("--kb")
    p.add_argument("--brief", action="store_true", help="只看元数据与摘要")
    p.add_argument("--alias", help="当前连接别名:顺带判断这条对本实例适不适用")
    p.add_argument("--section", help="官方文档条目:只看这一小节")
    p.add_argument("--grep", help="官方文档条目:只看含这个词的行(错误码页按码号或报错原文)")
    p.add_argument("--full", action="store_true", help="官方文档条目:给全文(长条目默认只给摘要与小节清单)")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("doc", help="L1 官方文档集的目录导航(按原书目录下钻)")
    p.add_argument("code", nargs="?", help="文档集代号,如 G25132C;不给就列已装的文档集")
    p.add_argument("chapter", nargs="?", help="章节名(目录里任意一级),如 锁管理")
    p.add_argument("--topic", help="只看某专题下的条目")
    p.add_argument("--all", action="store_true", help="不截断,全部列出")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_doc)
    p = sub.add_parser("evidence", help="L3 一条条目的原始证据(核对细节或处理冲突时)")
    p.add_argument("id")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_evidence)
    p = sub.add_parser("template", help="新条目的骨架(导入时照着写,再 kb.py add)")
    p.add_argument("type", choices=sorted(kbtypes.TYPED))
    p.set_defaults(func=cmd_template)


if __name__ == "__main__":                              # pragma: no cover  只经 kb.py 调用
    sys.exit("请通过 kb.py 调用")
