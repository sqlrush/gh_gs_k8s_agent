#!/usr/bin/env python3
"""gaussdb-kb-init —— 入库前的标准化:五花八门的工单材料 → 统一格式的六要素 md。

    scan <路径...>   建批次:快照原件、分类、能抽的抽成文本、逐单开工作单
    (你写草稿)        按工作单把六要素写进 draft/NNN.json —— 照片要用 read 工具看图
    render           草稿 → std/*.md(统一格式)+ out/*.md(交给 kb.py ingest)
    validate         校验 std/*.md 还是不是标准格式
    status / list    进度 / 批次列表

退出码:0 成功;1 运行错误;2 有活没干完(草稿没写完、校验有问题)——与 gaussdb-kb-import 一致。

本脚本不连数据库、不读凭据,只搬文件与文本。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
from typing import Dict, List, Optional, Tuple

_HERE = pathlib.Path(__file__).resolve().parent
for _cand in (_HERE.parents[2], _HERE.parents[1]):        # skills/<name>/scripts → 安装根
    if (_cand / "common").is_dir():
        sys.path.insert(0, str(_cand))
        break

from common.kb import extract as kbextract  # noqa: E402
from common.kb import imgconv  # noqa: E402
from common.kb import ingest as kbingest  # noqa: E402
from common.kb import initstd  # noqa: E402
from common.kb.atomic import write_text_atomic  # noqa: E402


class InitError(Exception):
    """用户能改的错(路径不存在、批次不存在)。"""


# ---------------------------------------------------------------- 目录

def resolve_kb_dir(explicit: Optional[str]) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit).expanduser()
    env = os.environ.get("GSDB_KB_DIR")
    if env:
        return pathlib.Path(env).expanduser()
    return _HERE.parents[2] / "kb"


def batch_dir(kb: pathlib.Path, slug: str) -> pathlib.Path:
    return kb / "init" / slug


def _load_manifest(batch: pathlib.Path) -> Dict:
    path = batch / "manifest.json"
    if not path.is_file():
        raise InitError("批次 %s 不存在(先跑 scan)" % batch.name)
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_batch(kb: pathlib.Path) -> str:
    root = kb / "init"
    cands = sorted((p for p in root.glob("*/manifest.json")), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        raise InitError("还没有任何批次(先跑 scan <路径>)")
    return cands[0].parent.name


def _pick_batch(kb: pathlib.Path, explicit: Optional[str]) -> pathlib.Path:
    return batch_dir(kb, initstd.batch_slug(explicit) if explicit else _latest_batch(kb))


# ---------------------------------------------------------------- scan

def _iter_inputs(paths: List[str]) -> List[pathlib.Path]:
    out: List[pathlib.Path] = []
    for raw in paths:
        p = pathlib.Path(raw).expanduser()
        if p.is_dir():
            out += sorted(q for q in p.rglob("*") if q.is_file() and not q.name.startswith("."))
        elif p.is_file():
            out.append(p)
        else:
            raise InitError(
                "找不到 %s。用户给的是自己电脑上的路径时,skill 在沙箱里读不到——"
                "让他先把文件传到收件目录(%s),再用沙箱内的路径重试"
                % (raw, os.environ.get("GSDB_KB_INBOX") or "<kb>/inbox/uploads"))
    if not out:
        raise InitError("没有找到任何文件")
    return out


_TEMPLATE = {"item_id": "", "title": "", "occurred_at": "", "system": "", "severity": "",
             "conclusion": "已确认 或 推测", "objects": [], "signals": []}


def _template() -> Dict:
    tpl = dict(_TEMPLATE)
    for name in initstd.SECTIONS:
        tpl[name] = ""
    return tpl


def _table_work(path: pathlib.Path, snapshot_name: str) -> List[Dict]:
    """csv/xlsx:一行一单,列名能对上的字段先填好(仍由模型确认)。"""
    if path.suffix.lower() in (".xlsx", ".xls"):
        headers, rows = kbingest.read_xlsx(path)
    else:
        headers, rows = kbingest.read_csv(path)
    headers, rows = headers, kbingest.fit_rows(headers, rows, path.name)
    mapping = kbingest.guess_columns(headers)
    items = kbingest.rows_to_items(headers, rows, mapping, snapshot_name)
    suggest_map = {"现场": "description", "判断": "root_cause", "处置": "solution"}
    out: List[Dict] = []
    for it in items:
        fields = it.fields or {}
        get = lambda fld: str(fields.get(mapping.get(fld, ""), "") or "").strip()
        out.append({
            "source": it.locator,
            "text": it.text,
            "known": {k: v for k, v in (("item_id", it.id), ("system", get("system")),
                                        ("occurred_at", get("occurred_at")),
                                        ("severity", get("severity")), ("title", it.title)) if v},
            "suggest": {k: get(v) for k, v in suggest_map.items() if get(v)},
        })
    return out


def cmd_scan(args: argparse.Namespace) -> int:
    kb = resolve_kb_dir(args.kb)
    files = _iter_inputs(args.paths)
    slug = initstd.batch_slug(args.batch or (pathlib.Path(args.paths[0]).stem
                                             if len(args.paths) == 1 else "material"))
    batch = batch_dir(kb, slug)
    for sub in ("sources", "work", "draft", "std", "out"):
        (batch / sub).mkdir(parents=True, exist_ok=True)

    kept_drafts = sorted(p.name for p in (batch / "draft").glob("*.json"))
    manifest: Dict = {"batch": slug, "kb": str(kb), "files": []}
    work_no = 0
    images: List[str] = []
    unsupported: List[str] = []
    view_of: Dict[str, pathlib.Path] = {}     # 原件名 → 给 read 看的副本(HEIC / TIFF 转出的 jpg)

    for src in files:
        c = initstd.classify(src)
        snapshot = batch / "sources" / src.name
        if not snapshot.exists() or snapshot.stat().st_size != src.stat().st_size:
            shutil.copy2(src, snapshot)
        entry = {"name": src.name, "kind": c.kind, "reason": c.reason,
                 "snapshot": str(snapshot.relative_to(batch)), "work": []}

        orders: List[Dict] = []
        if c.kind in ("text", "doc"):
            try:
                text = kbextract.normalize_text(kbextract.extract_source(src))
            except kbextract.ExtractError as exc:
                entry["kind"] = "unsupported"
                entry["reason"] = str(exc)
                unsupported.append("%s —— %s" % (src.name, str(exc).splitlines()[0]))
                manifest["files"].append(entry)
                continue
            orders = [{"source": src.name, "text": text, "known": {}, "suggest": {}}]
        elif c.kind == "table":
            try:
                orders = _table_work(src, src.name)
            except kbingest.IngestError as exc:
                entry["kind"] = "unsupported"
                entry["reason"] = str(exc)
                unsupported.append("%s —— %s" % (src.name, exc))
                manifest["files"].append(entry)
                continue
        elif c.kind == "image":
            # HEIC / TIFF 先转成 jpg 副本:read 打不开原件(2026-10-22 用户反馈一批 HEIC 一张没读出来)
            readable, note = imgconv.readable_copy(snapshot, snapshot.parent)
            if readable is not None:
                view_of[src.name] = readable
                entry["readable"] = str(readable.relative_to(batch))
            orders = [{"source": src.name, "text": None, "known": {}, "suggest": {}}]
            images.append("%s —— %s" % (src.name, note or c.reason))
        else:
            unsupported.append("%s —— %s" % (src.name, c.reason))
            manifest["files"].append(entry)
            continue

        for order in orders:
            work_no += 1
            wid = "%03d" % work_no
            payload = {
                "work_id": wid,
                "source_file": src.name,
                "source": order["source"],
                "source_kind": src.suffix.lower().lstrip(".") or "unknown",
                "extracted_by": "model" if c.kind == "image" else "script",
                "image_path": str(view_of.get(src.name, snapshot)) if c.kind == "image" else None,
                "text": order["text"],
                "known": order["known"],
                "suggest": order["suggest"],
                "draft_path": "draft/%s.json" % wid,
                "template": _template(),
            }
            write_text_atomic(batch / "work" / ("%s.json" % wid),
                              json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            entry["work"].append(wid)
        manifest["files"].append(entry)

    write_text_atomic(batch / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    print("批次        : %s(%s)" % (slug, batch))
    print("材料        : %d 个文件 → %d 个工作单" % (len(manifest["files"]), work_no))
    if kept_drafts:
        print("已有草稿    : %d 份(未动:%s)" % (len(kept_drafts), "、".join(kept_drafts[:5])))
    if images:
        print()
        print("需要你看图的(脚本 OCR 不了,用 read 工具逐张读):")
        for line in images:
            print("  - " + line)
    if unsupported:
        print()
        print("没能处理的(留在清单里,别当成已导入):")
        for line in unsupported:
            print("  - " + line)
    print()
    print("Next: 逐个读 %s/work/*.json,按 template 把六要素写进 draft/NNN.json,"
          "然后跑 render。" % batch)
    return 2 if work_no else 0


# ---------------------------------------------------------------- render

def _read_drafts(path: pathlib.Path) -> List[Dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else [raw]


def cmd_render(args: argparse.Namespace) -> int:
    kb = resolve_kb_dir(args.kb)
    batch = _pick_batch(kb, args.batch)
    manifest = _load_manifest(batch)
    work_files = sorted((batch / "work").glob("*.json"))
    if not work_files:
        raise InitError("批次 %s 里没有工作单" % batch.name)

    pairs: List[Tuple[Dict, Dict]] = []
    problems: List[str] = []
    for wf in work_files:
        work = json.loads(wf.read_text(encoding="utf-8"))
        wid = work["work_id"]
        draft_path = batch / "draft" / ("%s.json" % wid)
        if not draft_path.is_file():
            problems.append("%s(%s):草稿还没写 —— %s" % (wid, work["source_file"], draft_path))
            continue
        try:
            drafts = _read_drafts(draft_path)
        except ValueError as exc:
            problems.append("%s(%s):草稿不是合法 JSON(%s)" % (wid, work["source_file"], exc))
            continue
        for n, draft in enumerate(drafts, 1):
            meta = {"source": draft.get("source") or work["source"],
                    "source_file": work["source_file"],
                    "source_kind": work["source_kind"],
                    "extracted_by": work["extracted_by"],
                    "redacted": draft.get("redacted", 0)}
            try:
                initstd.render_std(draft, meta)          # 先验后写:有问题的一份都不落盘
            except initstd.StdFormatError as exc:
                where = wid if len(drafts) == 1 else "%s#%d" % (wid, n)
                problems.append("%s(%s):%s" % (where, work["source_file"], exc))
                continue
            pairs.append((draft, meta))

    if problems:
        print("还不能渲染,%d 处要先补:" % len(problems))
        for p in problems:
            print("  - " + p)
        print()
        print("（六要素缺一不可;原文确实没写的,写「原文未写明」而不是留空。）")
        return 2

    for old in (batch / "std").glob("*.md"):
        old.unlink()
    written = []
    taken: set = set()
    for draft, meta in pairs:
        text = initstd.render_std(draft, meta)
        fm, _ = initstd.parse_std(text)
        # 文件名 = 日期 + 工单号;没有工单号的材料(问题分析报告就没有)用标题。同名再加序号——
        # 同一天两单没号的要是都叫「日期-unknown」,后写的会盖掉先写的,一单就这么没了(模型级 e2e 抓到的)。
        ident = fm["item_id"] if fm["item_id"] not in ("", "unknown") else fm["title"]
        base = initstd.batch_slug("%s-%s" % (fm["occurred_at"], ident))
        name, n = base, 2
        while name in taken:
            name, n = "%s-%d" % (base, n), n + 1
        taken.add(name)
        path = batch / "std" / ("%s.md" % name)
        write_text_atomic(path, text)
        written.append(path)

    out_file = batch / "out" / ("%s-tickets.md" % manifest["batch"])
    write_text_atomic(out_file, initstd.render_ingest_file(pairs))

    print("统一格式    : %d 份 → %s" % (len(written), (batch / "std")))
    for p in written[:10]:
        print("  - " + p.name)
    if len(written) > 10:
        print("  …… 共 %d 份" % len(written))
    print("待导入文件  : %s" % out_file)
    print()
    print("Next: 交给知识库导入 skill(仍要走 propose → 选择列表 → apply 的确认闸门):")
    print("    python3 %s ingest %s --kind tickets" % (_import_script(), out_file))
    return 0


def _import_script() -> str:
    """导入命令所在的脚本:拆分部署在 gaussdb-kb-import,未拆分的在 gaussdb-kb。按实际存在的那个给路径。"""
    skills = _HERE.parents[1]
    for name in ("gaussdb-kb-import", "gaussdb-kb"):
        cand = skills / name / "scripts" / "kb.py"
        if cand.is_file():
            return str(cand)
    return str(skills / "gaussdb-kb-import" / "scripts" / "kb.py")


# ---------------------------------------------------------------- validate / status / list

def cmd_validate(args: argparse.Namespace) -> int:
    kb = resolve_kb_dir(args.kb)
    batch = _pick_batch(kb, args.batch)
    _load_manifest(batch)
    files = sorted((batch / "std").glob("*.md"))
    if not files:
        print("批次 %s 还没有渲染出统一格式文件(先跑 render)" % batch.name)
        return 2
    bad = 0
    for path in files:
        problems = initstd.validate_std(path.read_text(encoding="utf-8"))
        if problems:
            bad += 1
            print("[error] %s" % path.name)
            for p in problems:
                print("        - " + p)
    print("校验: %d 份,%d 份有问题(KB=%s)" % (len(files), bad, kb))
    return 2 if bad else 0


def cmd_status(args: argparse.Namespace) -> int:
    kb = resolve_kb_dir(args.kb)
    batch = _pick_batch(kb, args.batch)
    manifest = _load_manifest(batch)
    work = sorted((batch / "work").glob("*.json"))
    drafts = {p.stem for p in (batch / "draft").glob("*.json")}
    std = list((batch / "std").glob("*.md"))
    need_vision = [json.loads(p.read_text(encoding="utf-8")) for p in work]
    vision_left = [w for w in need_vision if w["extracted_by"] == "model" and w["work_id"] not in drafts]
    unsupported = [f for f in manifest["files"] if f["kind"] == "unsupported"]

    print("批次        : %s(%s)" % (manifest["batch"], batch))
    print("工作单      : %d,已写草稿 %d/%d" % (len(work), len(drafts), len(work)))
    print("待写草稿    : %d" % (len(work) - len(drafts)))
    if vision_left:
        print("其中要看图  : %d(%s)" % (len(vision_left), "、".join(w["source_file"] for w in vision_left[:5])))
    print("统一格式    : %d 份" % len(std))
    if unsupported:
        print("未处理材料  : %d(%s)" % (len(unsupported), "、".join(f["name"] for f in unsupported[:5])))
    return 0 if std and len(drafts) == len(work) else 2


def cmd_list(args: argparse.Namespace) -> int:
    kb = resolve_kb_dir(args.kb)
    root = kb / "init"
    batches = sorted(root.glob("*/manifest.json")) if root.is_dir() else []
    if not batches:
        print("还没有任何批次(KB=%s)" % kb)
        return 0
    for mf in batches:
        m = json.loads(mf.read_text(encoding="utf-8"))
        b = mf.parent
        print("%-24s 材料 %2d  工作单 %2d  草稿 %2d  统一格式 %2d"
              % (m["batch"], len(m["files"]), len(list((b / "work").glob("*.json"))),
                 len(list((b / "draft").glob("*.json"))), len(list((b / "std").glob("*.md")))))
    return 0


# ---------------------------------------------------------------- main

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kb_init.py", description="入库前把各种工单材料标准化成统一格式 md")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_text: str, func, batch: str = "批次名(默认最近一次 scan 的批次)") -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--kb", help="KB 目录(默认 $GSDB_KB_DIR 或 <安装根>/kb)")
        if batch:
            p.add_argument("--batch", help=batch)
        p.set_defaults(func=func)
        return p

    add("scan", "建批次:快照、分类、抽文本、开工作单", cmd_scan,
        batch="批次名(默认取第一个路径的文件名)").add_argument("paths", nargs="+", help="文件或目录")
    add("render", "草稿 → 统一格式 md + 待导入文件", cmd_render)
    add("validate", "校验统一格式文件", cmd_validate)
    add("status", "进度", cmd_status)
    add("list", "列出所有批次", cmd_list, batch="")

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (InitError, initstd.StdFormatError) as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
