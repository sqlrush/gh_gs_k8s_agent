"""kb 官方文档集子命令(只在导入环境):把 GaussDB 官方文档(CHM)变成标准知识库的一层。

    docset scan <CHM> [--slug]           解包、读目录,出范围清单(kb.py ingest <CHM> 走的就是它)
    docset plan <批次> --code … --product … --versions … [--exclude 目录前缀] [--override "目录=专题,…"]
                                          写构建配方、出专题映射表与样例;改映射再跑一次 plan
    docset apply <批次>                  按配方写 docsets/<代号>/(原子换上;ID 永不复用)→ 然后 kb.py index
    docset summaries <代号> --next 10    取下一批重点页(待精修摘要);--apply <json> 核对后写回
    docset validate <代号> · list · retire <代号> · pack <代号> --out <目录> · install <包或目录>

分工:脚本切分、写条目、核对、打包;模型确认范围与专题映射、抽读样例、精修重点页摘要、验收检索。
确定性:同一份 CHM + 同一套 plan/ 重放 apply,结果逐条一致——发版本靠它,不再调用模型。
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import shutil
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

from common.kb import chmdoc
from common.kb import config as kbconfig
from common.kb import docindex as kbdocindex
from common.kb import docset as kbdocset
from common.kb import docset_pack as kbpack
from common.kb import docsplit
from common.kb import lock as kblock
from common.kb import store as spg
from common.kb import store_sqlite as ssq
from common.kb import topics as kbtopics
from common.kb.atomic import write_text_atomic

DEFAULT_BUDGET_S = 100
PRIORITY_CHAPTERS = ("故障处理", "SQL调优指南", "开发设计建议", "FAQ", "巡检指南", "开发设计规范")
_SUMMARY_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,}|\d+(?:\.\d+)?")


class DocsetCmdError(Exception):
    pass


def _kb(args: argparse.Namespace) -> pathlib.Path:
    kb = kbconfig.resolve_kb_dir(getattr(args, "kb", None))
    if not kb.is_dir():
        raise DocsetCmdError(f"知识库目录不存在:{kb}")
    return kb


def _staging(kb: pathlib.Path, slug: str) -> pathlib.Path:
    return kb / "inbox" / slug


def _slug_of(path: pathlib.Path) -> str:
    stem = re.sub(r"[^\w.-]+", "-", pathlib.Path(path).stem).strip("-")
    return "docset-" + (stem[:40] or "doc")


# ---------------------------------------------------------------- scan

def cmd_scan(args: argparse.Namespace) -> int:
    kb = _kb(args)
    src = pathlib.Path(args.file)
    slug = args.slug or _slug_of(src)
    staging = _staging(kb, slug)
    with kblock.hold(kb):
        res = kbdocset.scan(src, staging)
    print(f"文档包      : {res['source_file']} → 批次 {slug}(解包在 inbox/{slug}/)")
    print(f"目录        : {res['nodes']} 个节点 · {res['pages']} 页 · 书:{'、'.join(res['books'])}")
    if res["versions_seen"]:
        print("版本串      : " + "、".join(f"{v}({n} 处)" for v, n in res["versions_seen"]) + "(只是文中出现过的,版本以用户确认为准)")
    print("\n## 范围清单(一级 › 二级目录;默认全部导入,不导的给 --exclude)\n")
    print("| # | 目录 | 页 | 大小 |\n|---|---|---|---|")
    for i, r in enumerate(res["chapters"], 1):
        print(f"| {i} | {r['chapter']} | {r['pages']} | {r['bytes'] // 1024} KB |")
    print(f"""
## 要用户确认的(逐项问,不要替用户定)
1. 范围:全部导入,还是去掉哪几章(给编号)
2. 产品系列(GaussDB 集中式 / GaussDB 分布式 / openGauss)与适用版本范围(如 505.2);部署形态(如 华为云Stack 8.5.1)
3. 文档版本(如 25.1.32)、内核版本(如 505.2.1.SPC0800)、文档发布日期
4. 文档集代号(大写字母开头,2–10 位大写字母或数字,如 G25132C;以后换版另起代号)

确认后:kb.py docset plan {slug} --code <代号> --product "<产品系列>" --versions <版本> --deployment "<部署>" \\
          --doc-version <文档版本> --kernel-version <内核版本> --as-of <YYYY-MM-DD> [--exclude "<目录前缀>" …]""")
    return 2


# ---------------------------------------------------------------- plan

def _write_plan(plan_dir: pathlib.Path, args: argparse.Namespace, scan: Dict) -> Dict:
    path = plan_dir / "plan.yaml"
    plan: Dict = {}
    if path.is_file():
        plan = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fields = {"code": args.code, "product": args.product, "versions": args.versions, "deployment": args.deployment,
              "doc_version": args.doc_version, "kernel_version": args.kernel_version,
              "engine_version": args.engine_version, "as_of": args.as_of, "title": args.title}
    plan.update({k: v for k, v in fields.items() if v})
    plan.setdefault("title", pathlib.Path(scan.get("source_file", "")).stem)
    plan["source_file"] = scan.get("source_file", "")
    plan["source_sha256"] = scan.get("source_sha256", "")
    if args.exclude:
        plan["exclude"] = sorted(set(plan.get("exclude") or []) | set(args.exclude))
    plan.setdefault("exclude", [])
    plan.setdefault("priority_chapters", list(PRIORITY_CHAPTERS))
    plan_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
    return plan


def _merge_overrides(plan_dir: pathlib.Path, rows: List[Dict], new: Sequence[str]) -> List[Dict]:
    path = plan_dir / "topic-map.yaml"
    old = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else []
    kept = {r["chapter"]: r["override"] for r in old or [] if isinstance(r, dict) and "override" in r}
    valid = {t.slug for t in kbtopics.DEFAULT_TOPICS}
    for spec in new or []:
        chapter, _, tops = spec.partition("=")
        chapter = chapter.strip()
        topics = [t.strip() for t in tops.split(",") if t.strip()]
        bad = [t for t in topics if t not in valid]
        if not chapter or bad:
            raise DocsetCmdError(f"--override 写法:\"目录 › 子目录=lock,param\";专题只能是 {'、'.join(sorted(valid))}"
                                 + (f"(不认识 {'、'.join(bad)})" if bad else ""))
        kept[chapter] = topics
    out = []
    for r in rows:
        row = {"chapter": r["chapter"], "items": r["items"], "topics": r["topics"]}
        if r["chapter"] in kept:
            row["override"] = kept.pop(r["chapter"])
        out.append(row)
    out += [{"chapter": c, "items": 0, "topics": {}, "override": t} for c, t in kept.items()]
    write_text_atomic(path, yaml.safe_dump(out, allow_unicode=True, sort_keys=False))
    return out


def cmd_plan(args: argparse.Namespace) -> int:
    kb = _kb(args)
    staging = _staging(kb, args.slug)
    root = kbdocset.unpacked_root(staging)
    scan = json.loads((staging / "scan.json").read_text(encoding="utf-8"))
    plan_dir = staging / "plan"
    with kblock.hold(kb):
        plan = _write_plan(plan_dir, args, scan)
        try:
            kbdocset.read_plan(plan_dir)
        except kbdocset.DocsetError as exc:
            raise DocsetCmdError(str(exc))
        nodes = chmdoc.load_toc(root)
        items, _pages = kbdocset.split_all(root, nodes, plan["exclude"], kbtopics.DEFAULT_TOPICS)
        raw_rows = kbdocset.topic_map_rows(items, depth=args.depth)
        rows = _merge_overrides(plan_dir, raw_rows, args.override)
        items = docsplit.apply_override(items, kbdocset.overrides(plan_dir))
    kinds: Dict[str, int] = {}
    for it in items:
        kinds[it.kind] = kinds.get(it.kind, 0) + 1
    print(f"配方        : inbox/{args.slug}/plan/plan.yaml · 代号 {plan['code']} · {plan['product']} {plan['versions']}"
          + (f" · {plan['deployment']}" if plan.get("deployment") else ""))
    print(f"条目        : {len(items)}(GUC 参数 {kinds.get('guc', 0)} · 页面 {kinds.get('page', 0)} · 错误码页 {kinds.get('errcode', 0)})"
          + (f" · 排除 {len(plan['exclude'])} 个目录" if plan["exclude"] else ""))
    names = {t.slug: t.name for t in kbtopics.DEFAULT_TOPICS}
    print("\n## 专题映射表(按目录;核对后给用户确认;要改:--override \"目录=专题,…\",空 = 不归专题)\n")
    print("| # | 目录 | 条目 | 专题分布 | 覆盖 |\n|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        dist = " ".join(f"{names.get(k, k)}{v}" for k, v in sorted(r["topics"].items(), key=lambda kv: -kv[1])[:4])
        print(f"| {i} | {r['chapter']} | {r['items']} | {dist} | {','.join(r['override']) if 'override' in r else ''} |")
    print("\n## 样例(每类一条,检查切分与摘要)\n")
    for kind in (docsplit.KIND_GUC, docsplit.KIND_PAGE, docsplit.KIND_ERRCODE):
        it = next((x for x in items if x.kind == kind and x.chars > 300), None)
        if it is not None:
            text = kbdocset.render_item(plan, it, f"PD-{plan['code']}-?????")
            print("```markdown\n" + text[:1500] + ("\n…" if len(text) > 1500 else "") + "\n```\n")
    print(f"用户确认映射与样例后:kb.py docset apply {args.slug}")
    return 2


# ---------------------------------------------------------------- apply

def cmd_apply(args: argparse.Namespace) -> int:
    kb = _kb(args)
    staging = _staging(kb, args.slug)
    root = kbdocset.unpacked_root(staging)
    plan_dir = pathlib.Path(args.plan_dir) if args.plan_dir else staging / "plan"
    with kblock.hold(kb):
        try:
            m = kbdocset.apply(kb, root, plan_dir)
        except kbdocset.DocsetError as exc:
            raise DocsetCmdError(str(exc))
    print(f"文档集      : docsets/{m['code']}/ · {m['items']} 条 · 原页 {m['pages']} · {m.get('title', '')}")
    print(f"类别        : " + " · ".join(f"{k} {v}" for k, v in m["kinds"].items()))
    print(f"专题        : " + " · ".join(f"{k} {v}" for k, v in sorted(m["topics"].items(), key=lambda kv: -kv[1])))
    print("下一步      : kb.py index(带时限,没建完退出 2,重复跑到退出 0)→ kb.py docset validate "
          f"{m['code']} → 验收检索;批次处理完 kb.py inbox-clean {args.slug}")
    return 0


# ---------------------------------------------------------------- index(kb.py index 末尾调用)

def index_docsets(kb: pathlib.Path, budget_s: float, rebuild: bool = False) -> int:
    """逐套建 / 补官方文档集索引;本库嵌入模型与该套记录的不同时整套重算。返回 0 = 都完整,2 = 还有活。"""
    import time
    from common.kb.embed import Embedder, EmbedError
    sets = kbdocset.installed(kb)
    if not sets:
        return 0
    cfg = kbconfig.load(kb)
    try:
        embedder = Embedder.from_config(cfg)
    except (EmbedError, kbconfig.KbConfigError) as exc:
        print(f"官方文档集  : embedding 未启用({exc}),只建关键词")
        embedder = None
    started = time.monotonic()
    rc = 0
    for ds in sets:
        left = max(5.0, budget_s - (time.monotonic() - started)) if budget_s else 0.0
        try:
            store = ssq.SqliteStore.open_writer(ds.index_path, dims=cfg.embeddings.dims, fresh=rebuild)
        except spg.StoreError as exc:
            print(f"官方文档集 {ds.code}: 写索引失败:{exc}")
            rc = 2
            continue
        try:
            recorded = store.meta_get("embed_model") if store.has_index() else None
            again = rebuild or bool(embedder and recorded and recorded != embedder.model)
            if again and recorded and embedder and recorded != embedder.model:
                print(f"官方文档集 {ds.code}: 向量是 {recorded} 算的,本库配的是 {embedder.model},整套按本库模型重算")
            rep = kbdocindex.run(ds, store, embedder, budget_s=left, rebuild=again)
            store.publish()
        except spg.StoreError as exc:
            print(f"官方文档集 {ds.code}: 写索引失败:{exc}")
            rc = 2
            continue
        finally:
            store.close()
        kbdocset.update_index_state(ds.root, kbdocindex.state_of(rep))
        print(f"官方文档集 {ds.code}: 条目 {rep.docs_total} · 新写 {rep.docs_indexed} · 未变 {rep.docs_unchanged} · "
              f"剩 {rep.docs_left} · 向量 {rep.chunk_embedded}/{rep.chunk_total} · 只建关键词 {rep.lexical_only} · "
              f"{rep.elapsed_s}s")
        if not rep.done:
            rc = 2
            print(f"            : 没建完(时限 {int(budget_s)}s 到了或向量没补齐)——再跑一次 `kb.py index` 接着来")
    return rc


# ---------------------------------------------------------------- 摘要精修

def _priority(entry: Dict, chapters: Sequence[str]) -> bool:
    path = entry.get("path") or []
    if entry.get("kind") != docsplit.KIND_PAGE:
        return False
    if "开发指南" in path and "最佳实践" in path:
        return True
    return any(c in path for c in chapters)


def _refine_targets(ds: kbdocset.Docset) -> List[Dict]:
    plan = yaml.safe_load((ds.root / "plan" / "plan.yaml").read_text(encoding="utf-8")) or {}
    chapters = plan.get("priority_chapters") or PRIORITY_CHAPTERS
    return [c for c in ds.catalog() if _priority(c, chapters)]


def check_summary(summary: str, source: str) -> List[str]:
    """模型写的摘要里出现的英文标识、数字都必须在原页里找得到;找不到的列出来(整条退回)。"""
    s = summary.strip()
    problems = []
    if not s:
        return ["摘要是空的"]
    if len(s) > docsplit.SUMMARY_MAX:
        problems.append(f"超过 {docsplit.SUMMARY_MAX} 字({len(s)})")
    low = source.lower()
    missing = [t for t in dict.fromkeys(_SUMMARY_TOKEN_RE.findall(s)) if t.lower() not in low]
    if missing:
        problems.append("原页里找不到:" + "、".join(missing[:8]))
    return problems


def _write_summary(ds: kbdocset.Docset, entry: Dict, summary: str) -> None:
    path = ds.item_path(entry["id"])
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^summary_by: \w+$", "summary_by: model", text, count=1, flags=re.M)
    text = re.sub(r"(## 摘要\n)(.*?)(\n\n## |\n*$)", lambda m: m.group(1) + summary + m.group(3), text, count=1, flags=re.S)
    path.write_text(text, encoding="utf-8")


def cmd_summaries(args: argparse.Namespace) -> int:
    kb = _kb(args)
    ds = kbdocset.get(kb, args.code)
    targets = _refine_targets(ds)
    todo = [c for c in targets if c.get("summary_by") != "model"]
    if args.apply:
        data = json.loads(pathlib.Path(args.apply).read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in ds.catalog()}
        ok, bad = [], []
        for item_id, summary in (data.items() if isinstance(data, dict) else []):
            entry = by_id.get(item_id)
            if entry is None:
                bad.append((item_id, ["不是本文档集的条目"]))
                continue
            _meta, secs = kbdocindex.parse_item(ds.item_path(item_id).read_text(encoding="utf-8"))
            problems = check_summary(str(summary), "\n".join(v for k, v in secs.items() if k != "摘要") + entry["title"])
            (bad if problems else ok).append((item_id, problems or str(summary).strip()))
        with kblock.hold(kb):
            store_file = ds.root / "plan" / "summaries.yaml"
            saved = yaml.safe_load(store_file.read_text(encoding="utf-8")) if store_file.is_file() else {}
            saved = saved or {}
            cat = ds.catalog()
            for item_id, summary in ok:
                entry = by_id[item_id]
                _write_summary(ds, entry, summary)
                saved[entry["doc_key"]] = {"summary": summary, "by": "model",
                                           "at": datetime.date.today().isoformat()}
                for c in cat:
                    if c["id"] == item_id:
                        c["summary"], c["summary_by"] = summary, "model"
            write_text_atomic(store_file, yaml.safe_dump(saved, allow_unicode=True, sort_keys=False))
            write_text_atomic(ds.root / "catalog.json", json.dumps(cat, ensure_ascii=False))
            m = dict(ds.manifest)
            m["model_summaries"] = sum(1 for c in cat if c.get("summary_by") == "model")
            write_text_atomic(ds.root / "manifest.json", json.dumps(m, ensure_ascii=False, indent=1))
        print(f"写回 {len(ok)} 条;退回 {len(bad)} 条")
        for item_id, problems in bad:
            print(f"[退回] {item_id}:{';'.join(problems)}")
        left = len(todo) - len(ok)
        print(f"重点页还剩 {max(left, 0)} 条待精修" + ("(再 --next 取下一批)" if left > 0 else ";全部完成,跑 kb.py index"))
        return 2 if bad or left > 0 else 0
    if args.status or not args.next:
        print(f"重点页 {len(targets)} 条 · 已精修 {len(targets) - len(todo)} · 待精修 {len(todo)}")
        return 0
    batch = todo[:args.next]
    print(f"# 待精修摘要(本批 {len(batch)} 条 / 剩 {len(todo)} 条)\n")
    print("要求:每条 ≤200 字、结论在前(这页能回答什么问题、关键判断与做法);只写原页里有的,"
          "参数名、视图名、等待事件名、数值必须原样出自原页(脚本逐个核对,找不到整条退回)。\n"
          f"写成 JSON {{\"<ID>\": \"<摘要>\", …}} 存到文件,再 kb.py docset summaries {ds.code} --apply <文件>\n")
    for c in batch:
        _meta, secs = kbdocindex.parse_item(ds.item_path(c["id"]).read_text(encoding="utf-8"))
        body = "\n".join(f"### {k}\n{v}" for k, v in secs.items() if k != "摘要")
        print(f"## {c['id']}《{c['title']}》\n> 目录:{' › '.join(c.get('path') or [])}\n> 现有摘要:{c['summary']}\n")
        print(body[:args.chars] + ("\n…(原页更长,只给前面部分)" if len(body) > args.chars else "") + "\n")
    return 2


# ---------------------------------------------------------------- validate / list / retire / pack / install

def validate(kb: pathlib.Path, ds: kbdocset.Docset) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warns: List[str] = []
    cat = ds.catalog()
    valid_topics = {t.slug for t in kbtopics.DEFAULT_TOPICS}
    if ds.manifest.get("items") != len(cat):
        errors.append(f"manifest 记 {ds.manifest.get('items')} 条,catalog.json 有 {len(cat)} 条")
    ids = set()
    for c in cat:
        item_id = str(c.get("id"))
        if not kbdocset.ID_RE.match(item_id) or kbdocset.code_of(item_id) != ds.code:
            errors.append(f"{item_id}: 编号不合规(应为 PD-{ds.code}-NNNNN)")
        if item_id in ids:
            errors.append(f"{item_id}: 编号重复")
        ids.add(item_id)
        path = ds.item_path(item_id)
        if not path.is_file():
            errors.append(f"{item_id}: 条目文件不存在")
            continue
        meta, secs = kbdocindex.parse_item(path.read_text(encoding="utf-8"))
        if meta.get("id") != item_id:
            errors.append(f"{item_id}: 文件里的 id 是 {meta.get('id')}")
        if not secs.get("摘要"):
            errors.append(f"{item_id}: 没有摘要")
        elif len(secs["摘要"]) > docsplit.SUMMARY_MAX:
            warns.append(f"{item_id}: 摘要 {len(secs['摘要'])} 字,超过 {docsplit.SUMMARY_MAX}")
        bad = [t for t in meta.get("topics") or [] if t not in valid_topics]
        if bad:
            errors.append(f"{item_id}: 专题 {'、'.join(bad)} 不在默认专题里(标准知识库只用默认专题)")
        if not (ds.root / str(meta.get("page") or "")).is_file():
            errors.append(f"{item_id}: 原页 {meta.get('page')} 不存在")
    return errors, warns


def cmd_validate(args: argparse.Namespace) -> int:
    kb = _kb(args)
    ds = kbdocset.get(kb, args.code)
    errors, warns = validate(kb, ds)
    for e in errors[:50]:
        print(f"[error] {e}")
    for w in warns[:20]:
        print(f"[warn ] {w}")
    idx = ds.manifest.get("index") or {}
    print(f"文档集 {ds.code}: {ds.manifest.get('items')} 条 · error {len(errors)} · warn {len(warns)} · "
          f"索引{'完整' if idx.get('complete') else '未完成'}")
    return 2 if errors else 0


def cmd_list(args: argparse.Namespace) -> int:
    kb = _kb(args)
    sets = kbdocset.installed(kb)
    if not sets:
        print("(没有官方文档集)")
        return 0
    for ds in sets:
        m, idx = ds.manifest, ds.manifest.get("index") or {}
        print(f"{ds.code}: {m.get('title', '')} · {m.get('product', '')} {m.get('versions', '')} · {m.get('items')} 条 · "
              f"精修摘要 {m.get('model_summaries', 0)} · 索引{'完整' if idx.get('complete') else '未完成'} · "
              f"内容版本 {kbpack.content_version(m)}")
    return 0


def cmd_retire(args: argparse.Namespace) -> int:
    kb = _kb(args)
    with kblock.hold(kb):
        dest = kbdocset.retire(kb, args.code)
    print(f"{args.code} 已退役:移到 {dest.relative_to(kb)}(编号保留、永不复用);跑 kb.py index 重建分层")
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    kb = _kb(args)
    ds = kbdocset.get(kb, args.code)
    cfg = kbconfig.load(kb)
    errors, _w = validate(kb, ds)
    why = kbpack.preflight(kb, ds, cfg.docset_applicability_unknown, errors)
    if why:
        for w in why:
            print(f"[拒绝] {w}")
        return 2
    out = kbpack.pack(ds, pathlib.Path(args.out))
    print(f"标准知识库包: {out}({out.stat().st_size // (1 << 20)} MB)· 内容版本 {kbpack.content_version(ds.manifest)}")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    kb = _kb(args)
    src = pathlib.Path(args.pack)
    packs = kbpack.find_packs(src) if src.is_dir() else [src]
    if not packs:
        print(f"{src} 里没有标准知识库包(kb-std-*.tar.gz),不用装")
        return 0
    rc = 0
    with kblock.hold(kb):
        for p in packs:
            try:
                result, note = kbpack.install(kb, p)
            except kbpack.PackError as exc:
                print(f"[error] {p.name}: {exc}")
                rc = 1
                continue
            print(f"[{result}] {note}")
        if rc == 0:
            from common.kb import layers as kblayers
            version = (kb / "VERSION").read_text(encoding="utf-8").strip() if (kb / "VERSION").is_file() else ""
            kblayers.write_layers(kb, version)
            print("分层        : L0 / L1 已按装好的文档集重建")
    return rc


# ---------------------------------------------------------------- wiring

def add_subcommands(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("docset", help="官方文档集(CHM → 标准知识库):scan / plan / apply / summaries / validate / pack / install")
    s = p.add_subparsers(dest="docset_cmd", required=True)
    q = s.add_parser("scan", help="解包、读目录,出范围清单")
    q.add_argument("file")
    q.add_argument("--slug")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_scan)
    q = s.add_parser("plan", help="写构建配方(元数据、范围、专题覆盖),出专题映射表与样例")
    q.add_argument("slug")
    for name in ("code", "product", "versions", "deployment", "doc-version", "kernel-version", "engine-version",
                 "as-of", "title"):
        q.add_argument(f"--{name}")
    q.add_argument("--exclude", action="append", help="不导的目录前缀(用 › 连接),可多次")
    q.add_argument("--override", action="append", help="专题覆盖:\"目录 › 子目录=lock,param\",可多次")
    q.add_argument("--depth", type=int, default=3, help="映射表按第几级目录分行(默认 3)")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_plan)
    q = s.add_parser("apply", help="按配方写 docsets/<代号>/")
    q.add_argument("slug")
    q.add_argument("--plan-dir", help="用别处的配方(重放构建用,如 docsets/<代号>/plan)")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_apply)
    q = s.add_parser("summaries", help="重点页摘要精修:--next N 取一批;--apply <json> 核对后写回")
    q.add_argument("code")
    q.add_argument("--next", type=int, default=0)
    q.add_argument("--chars", type=int, default=2500, help="每页给多少字原文(默认 2500)")
    q.add_argument("--apply")
    q.add_argument("--status", action="store_true")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_summaries)
    for name, fn, hlp in (("validate", cmd_validate, "校验一套文档集"), ("retire", cmd_retire, "整套退役(换版时)")):
        q = s.add_parser(name, help=hlp)
        q.add_argument("code")
        q.add_argument("--kb")
        q.set_defaults(func=fn)
    q = s.add_parser("list", help="列出已装的文档集")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_list)
    q = s.add_parser("pack", help="打标准知识库包(测试开关关着、索引完整、校验零错误才出包)")
    q.add_argument("code")
    q.add_argument("--out", required=True)
    q.add_argument("--kb")
    q.set_defaults(func=cmd_pack)
    q = s.add_parser("install", help="装标准知识库包(同版本跳过;部署时自动调用)")
    q.add_argument("pack", help="包文件,或放包的目录")
    q.add_argument("--kb")
    q.set_defaults(func=cmd_install)


ERRORS = (DocsetCmdError, kbdocset.DocsetError, kbpack.PackError, chmdoc.ChmError)

if __name__ == "__main__":                              # pragma: no cover
    sys.exit("请通过 kb.py 调用")
