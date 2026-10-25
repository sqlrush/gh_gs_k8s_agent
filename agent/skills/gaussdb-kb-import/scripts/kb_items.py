"""kb 写命令:类型化条目入库、证据存档、改类型、存量迁移、画像起草、专题定义(只在导入环境)。

    add <草稿.md|目录>...      校验类型化条目草稿并放进对应类型目录(id: auto 时分配 ID);有 error 的拒收
    attach <文件>... [--to ID] 原始证据(照片、日志、报告)存进 evidence/,可顺手登记到某条的 evidence
    retype <ID> --to <类型>    换类型:换目录、换 ID,旧 ID 记进 aliases(报告里引用过的旧 ID 仍可追溯)
    migrate [--apply]          存量迁移:旧指南提议类型、条款 / 案例补专题;先出清单给人确认,再 --apply
    profile --system X ...     起草一份环境画像(连接别名、登录输出的版本串),管理员补全后 add
    taxonomy --init            写出可编辑的专题定义 topics/_taxonomy.yaml
    inbox-clean <批次>...      删掉处理完的 inbox/<批次>/(导入环境的命令行不放行 rm,删目录走这里)

分工与案例导入一致:模型起草、人确认、脚本校验落盘。所有写操作持知识库写锁。
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import shutil
from typing import Any, Dict, List, Optional, Tuple

import yaml

from common.kb import catalog as kbcat
from common.kb import config as kbconfig
from common.kb import lock as kblock
from common.kb import topics as kbtopics
from common.kb import types as kbtypes
from common.kb import versions as kbversions
from common.kb.atomic import write_text_atomic
from common.kb.cases import split_frontmatter

PLAN_FILE = "inbox/migrate-plan.yaml"
DRAFT_DIR = "inbox/drafts"
# 手册 ID 的域:没写 domain 时按第一个专题取
TOPIC_DOMAIN = {"conn": "CON", "lock": "LCK", "sql": "SQL", "proc": "PRC", "wait": "WAI", "wal": "WAL",
                "mem": "MEM", "vacuum": "VAC", "repl": "REP", "param": "GUC", "space": "SPC", "batch": "BAT"}
_PRODUCT_HINTS = ("产品文档", "官方文档", "产品手册", "开发指南", "管理员指南", "特性说明", "华为云", "产品描述")
_RUNBOOK_HINTS = ("步骤", "回退", "回滚", "操作手册", "处置流程", "应急", "执行前")


class ItemCmdError(Exception):
    """用户可读的错误。"""


def _kb(args: argparse.Namespace) -> pathlib.Path:
    kb = kbconfig.resolve_kb_dir(getattr(args, "kb", None))
    if not kb.is_dir():
        raise ItemCmdError(f"知识库目录不存在:{kb}")
    return kb


def compose(meta: Dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip() + "\n---\n" + body.lstrip("\n")


def _id_hint(type_code: str, meta: Dict[str, Any]) -> str:
    if type_code == "profile":
        return str(meta.get("system") or "")
    if type_code == "baseline":
        return f"{meta.get('system') or ''}/{meta.get('metric') or ''}"
    if type_code == "runbook":
        if meta.get("domain"):
            return str(meta["domain"])
        tops = kbtypes.as_list(meta.get("topics"))
        return TOPIC_DOMAIN.get(tops[0], "GEN") if tops else "GEN"
    return ""


# ---------------------------------------------------------------- add

def add_one(kb: pathlib.Path, draft: pathlib.Path, replace: bool) -> Tuple[Optional[kbtypes.Item], List[str], List[str]]:
    """(落盘后的条目, errors, warnings)。errors 非空时什么都不写。"""
    try:
        text = draft.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"读不了:{exc}(草稿必须是 UTF-8 文本)"], []
    meta, body, err = split_frontmatter(text)
    if err or not meta:
        return None, [err or "缺少 frontmatter(先 kb.py template <类型> 照着写)"], []
    code = str(meta.get("type") or "").strip()
    if code not in kbtypes.TYPED:
        return None, [f"type `{code or '(空)'}` 不是类型化条目;可选 {'/'.join(kbtypes.TYPED)}"
                      "(规范走 rules/、案例走 propose → review → apply)"], []
    kt = kbtypes.TYPED[code]
    item_id = str(meta.get("id") or "").strip()
    if item_id in ("", "auto"):
        try:
            item_id = kbtypes.next_id(kb, code, _id_hint(code, meta))
        except ValueError as exc:
            return None, [str(exc)], []
    meta = {**meta, "id": item_id}
    meta.pop("domain", None)
    target = kb / kt.dir / f"{item_id}.md"
    if target.exists() and not replace and target.resolve() != draft.resolve():
        return None, [f"{target.relative_to(kb)} 已存在(同一条要更新加 --replace;是新条目就把 id 写 auto)"], []
    final = compose(meta, body)
    item, errors, warns = kbtypes.parse_item(final, target, code)
    if errors:
        return None, errors, warns
    others = {e.id: e.file for e in kbcat.load(kb).entries}
    if item_id in others and others[item_id] != target.relative_to(kb).as_posix():
        return None, [f"ID `{item_id}` 已被 {others[item_id]} 占用"], warns
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, final)
    return item, [], warns


def _draft_files(paths: List[str]) -> List[pathlib.Path]:
    out: List[pathlib.Path] = []
    for raw in paths:
        p = pathlib.Path(raw).expanduser()
        if p.is_dir():
            out += sorted(x for x in p.rglob("*.md") if x.is_file())
        elif p.is_file():
            out.append(p)
        else:
            raise ItemCmdError(f"找不到 {raw}")
    return out


def cmd_add(args: argparse.Namespace) -> int:
    kb = _kb(args)
    drafts = _draft_files(args.files)
    ok, bad = [], 0
    with kblock.hold(kb):
        for d in drafts:
            item, errors, warns = add_one(kb, d, args.replace)
            if errors:
                bad += 1
                print(f"[拒收] {d}")
                for e in errors:
                    print(f"   - [error] {e}")
                continue
            ok.append(item)
            print(f"[入库] {item.path.relative_to(kb)}")
            for w in warns:
                print(f"   - [warn ] {w}")
            if args.move and d.resolve() != item.path.resolve():
                d.unlink()
    if ok:
        print("\n| 类型 | ID | 标题 | 专题 |\n|---|---|---|---|")
        for it in ok:
            print(f"| {kbtypes.TYPED[it.type].name} | {it.id} | {it.title} | {'、'.join(it.topics) or '—'} |")
    print(f"\nadd: 入库 {len(ok)} 条,拒收 {bad} 条。Next: python3 kb.py validate && python3 kb.py index")
    return 2 if bad else 0


# ---------------------------------------------------------------- attach

def _rewrite_front(path: pathlib.Path, update) -> None:
    text = path.read_text(encoding="utf-8")
    meta, body, err = split_frontmatter(text)
    if err or meta is None:
        raise ItemCmdError(f"{path.name} 的 frontmatter 坏了:{err}")
    write_text_atomic(path, compose(update(dict(meta)), body))


def cmd_attach(args: argparse.Namespace) -> int:
    kb = _kb(args)
    stamp = datetime.date.today().strftime("%Y%m%d")
    refs: List[str] = []
    with kblock.hold(kb):
        (kb / "evidence").mkdir(exist_ok=True)
        for raw in args.files:
            src = pathlib.Path(raw).expanduser()
            if not src.is_file():
                raise ItemCmdError(f"找不到 {raw}(先上传到收件目录)")
            dst = kb / "evidence" / f"{stamp}-{src.name}"
            n = 2
            while dst.exists():
                dst = kb / "evidence" / f"{stamp}-{src.stem}-{n}{src.suffix}"
                n += 1
            shutil.copy2(src, dst)
            refs.append(dst.relative_to(kb).as_posix())
            print(f"[存档] {refs[-1]}")
        if args.to:
            e = kbcat.load(kb).find(args.to)
            if e is None:
                raise ItemCmdError(f"没有条目 `{args.to}`")
            if e.type in (kbtypes.NORM, kbtypes.ERRATA):
                print(f"条款在 yaml 里:请把 evidence: {refs} 手工加到 {e.file} 的 {e.id} 下")
                return 0
            _rewrite_front(kb / e.file, lambda m: {**m, "evidence": list(dict.fromkeys(list(kbtypes.as_list(m.get("evidence"))) + refs))})
            print(f"已登记到 {e.id} 的 evidence({e.file})")
    return 0


# ---------------------------------------------------------------- guide / retype 转换

def guide_to_typed(kb: pathlib.Path, path: pathlib.Path, code: str, extra: Dict[str, Any]) -> pathlib.Path:
    """旧指南 → 类型化条目:description 作摘要,原正文放进「正文」;旧 id 记进 aliases。"""
    meta, body, err = split_frontmatter(path.read_text(encoding="utf-8"))
    if err or meta is None:
        raise ItemCmdError(f"{path.name}:{err}")
    old = str(meta.get("id") or path.stem)
    desc = str(meta.get("description") or old)
    new_meta: Dict[str, Any] = {"id": "auto", "type": code, "title": kbcat.first_sentence(desc, 40),
                                "topics": list(extra.get("topics") or []),
                                "source": meta.get("source") or path.relative_to(kb).as_posix(),
                                "aliases": list(dict.fromkeys(list(kbtypes.as_list(meta.get("aliases"))) + [old]))}
    for k in ("product", "versions", "deployment", "verification"):
        if extra.get(k):
            new_meta[k] = extra[k]
    if code in ("wiki", "runbook") and "verification" not in new_meta:
        new_meta["verification"] = "未验证"
    new_body = f"## 摘要\n{desc}\n\n## 正文\n{body.strip()}\n"
    new_meta["id"] = kbtypes.next_id(kb, code, _id_hint(code, new_meta))
    target = kb / kbtypes.TYPED[code].dir / f"{new_meta['id']}.md"
    text = compose(new_meta, new_body)
    _item, errors, _w = kbtypes.parse_item(text, target, code)
    if errors:
        raise ItemCmdError(f"{path.relative_to(kb)} → {code}:" + ";".join(errors))
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, text)
    path.unlink()
    return target


def cmd_retype(args: argparse.Namespace) -> int:
    kb = _kb(args)
    with kblock.hold(kb):
        e = kbcat.load(kb).find(args.id)
        if e is None:
            raise ItemCmdError(f"没有条目 `{args.id}`")
        if e.type == kbtypes.GUIDE:
            target = guide_to_typed(kb, kb / e.file, args.to, {"topics": list(e.topics)})
        elif e.type in kbtypes.TYPED:
            src = kb / e.file
            meta, body, _ = split_frontmatter(src.read_text(encoding="utf-8"))
            meta = dict(meta or {})
            meta.update({"type": args.to, "aliases": list(dict.fromkeys(list(kbtypes.as_list(meta.get("aliases"))) + [e.id]))})
            if args.domain:
                meta["domain"] = args.domain
            meta["id"] = kbtypes.next_id(kb, args.to, _id_hint(args.to, meta))
            meta.pop("domain", None)
            target = kb / kbtypes.TYPED[args.to].dir / f"{meta['id']}.md"
            text = compose(meta, body)
            _i, errors, warns = kbtypes.parse_item(text, target, args.to)
            if errors:
                raise ItemCmdError(f"改成 {args.to} 后校验不过:" + ";".join(errors) + "(先补齐该类型的必填项)")
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(target, text)
            src.unlink()
        else:
            raise ItemCmdError(f"{e.cite} 是{kbtypes.TYPE_NAMES[e.type][0]},不能改类型(条款在 rules/,案例在 cases/)")
    print(f"{e.id} → {target.relative_to(kb)}(旧 ID 已记进 aliases)。Next: python3 kb.py index")
    return 0


# ---------------------------------------------------------------- migrate

def _guess_guide_type(text: str) -> str:
    if any(h in text for h in _PRODUCT_HINTS):
        return "product"
    if sum(1 for h in _RUNBOOK_HINTS if h in text) >= 2:
        return "runbook"
    return "wiki"


def build_plan(kb: pathlib.Path) -> Dict[str, List[Dict[str, Any]]]:
    cat = kbcat.load(kb)
    plan: Dict[str, List[Dict[str, Any]]] = {"guides": [], "rules": [], "cases": []}
    for e in cat.of_type(kbtypes.GUIDE):
        try:
            text = (kb / e.file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        guess = _guess_guide_type(text)
        row: Dict[str, Any] = {"file": e.file, "old_id": e.id, "type": guess, "topics": list(e.topics)}
        if guess == "product":
            row.update({"product": "TODO", "versions": "TODO"})
        plan["guides"].append(row)
    for e in cat.of_type(kbtypes.NORM):
        if e.topics_inferred:
            plan["rules"].append({"id": e.id, "file": e.file, "topics": list(e.topics)})
    for e in cat.of_type(kbtypes.CASE):
        if e.topics_inferred:
            plan["cases"].append({"id": e.id, "file": e.file, "topics": list(e.topics)})
    return plan


def _insert_rule_topics(path: pathlib.Path, rid: str, topics: List[str]) -> bool:
    """在 `- id: GS-…` 那一行后面插一行 topics(不重写整个 yaml:文件首行注释是 INDEX 的说明,不能丢)。"""
    text = path.read_text(encoding="utf-8")
    rx = re.compile(r"^(\s*)-\s+id:\s*['\"]?" + re.escape(rid) + r"['\"]?\s*$", re.M)
    m = rx.search(text)
    if not m:
        return False
    nxt = re.compile(r"^\s*-\s+id:", re.M).search(text, m.end())
    block = text[m.end():nxt.start() if nxt else len(text)]
    if re.search(r"^\s+topics:", block, re.M):            # 已经有了:可重复执行,不重复插
        return False
    line = f"\n{m.group(1)}  topics: [{', '.join(topics)}]"
    write_text_atomic(path, text[:m.end()] + line + text[m.end():])
    return True


def _insert_case_topics(path: pathlib.Path, topics: List[str]) -> bool:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m or re.search(r"^topics:", m.group(1), re.M):
        return False
    pos = m.end(1)
    write_text_atomic(path, text[:pos] + f"\ntopics: [{', '.join(topics)}]" + text[pos:])
    return True


def apply_plan(kb: pathlib.Path, plan: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    done: List[str] = []
    problems: List[str] = []
    valid = {t.slug for t in kbtopics.load_taxonomy(kb)[0]}
    for row in plan.get("guides") or []:
        path = kb / str(row.get("file") or "")
        if not path.is_file():
            continue                                    # 已经迁过
        code = str(row.get("type") or "")
        if code not in ("wiki", "product", "runbook"):
            problems.append(f"{row.get('file')}: type 只能是 wiki / product / runbook")
            continue
        if code == "product" and ("TODO" in (str(row.get("product")), str(row.get("versions"))) or not row.get("versions")):
            problems.append(f"{row.get('file')}: 产品文档要填 product 与 versions(清单里还是 TODO)")
            continue
        topics = [t for t in kbtypes.as_list(row.get("topics")) if t in valid]
        if not topics:
            problems.append(f"{row.get('file')}: topics 为空或不在专题定义里")
            continue
        try:
            target = guide_to_typed(kb, path, code, {**row, "topics": topics})
            done.append(f"{row['file']} → {target.relative_to(kb).as_posix()}")
        except ItemCmdError as exc:
            problems.append(str(exc))
    for key, inserter in (("rules", None), ("cases", None)):
        for row in plan.get(key) or []:
            topics = [t for t in kbtypes.as_list(row.get("topics")) if t in valid]
            if not topics:
                continue                                # 留空 = 不归类(进「未归类」)
            path = kb / str(row.get("file") or "")
            if not path.is_file():
                continue
            ok = (_insert_rule_topics(path, str(row["id"]), topics) if key == "rules"
                  else _insert_case_topics(path, topics))
            if ok:
                done.append(f"{row['id']} topics = {topics}")
    return done, problems


def cmd_migrate(args: argparse.Namespace) -> int:
    kb = _kb(args)
    plan_path = pathlib.Path(args.plan).expanduser() if args.plan else kb / PLAN_FILE
    with kblock.hold(kb):
        if not args.apply:
            plan = build_plan(kb)
            head = ("# kb.py migrate 生成的迁移清单(存量 → 八类知识 + 专题)。逐条核对:\n"
                    "#   guides:旧指南,type 选 wiki / product / runbook;product 必须填 product(产品系列)与 versions\n"
                    "#   rules / cases:按同义词推断的专题,改对或删掉(删掉 = 不归类)\n"
                    "# 确认后运行:python3 kb.py migrate --apply\n")
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(head + yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8")
            print(f"迁移清单:{plan_path}(旧指南 {len(plan['guides'])} 篇,条款补专题 {len(plan['rules'])} 条,"
                  f"案例补专题 {len(plan['cases'])} 单)")
            for row in plan["guides"]:
                print(f"  - {row['file']} → {row['type']}  专题 {row['topics'] or '—'}")
            print("把清单给用户确认(可整批改类型 / 专题),然后 python3 kb.py migrate --apply")
            return 2 if any(plan.values()) else 0
        if not plan_path.is_file():
            raise ItemCmdError(f"没有迁移清单 {plan_path}(先运行 kb.py migrate 生成)")
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
        done, problems = apply_plan(kb, plan)
    for d in done:
        print(f"[迁移] {d}")
    for p in problems:
        print(f"[error] {p}")
    print(f"migrate: 完成 {len(done)} 项,问题 {len(problems)} 项。Next: python3 kb.py validate && python3 kb.py index")
    return 2 if problems else 0


# ---------------------------------------------------------------- profile / taxonomy

def parse_version_string(s: str) -> Tuple[str, str]:
    """`SELECT version()` 的输出 → (产品系列, 版本)。认不出就空串,不猜。"""
    s = s or ""
    # version() 的开头总是「PostgreSQL 9.2.4」(兼容口径),真正的产品在括号里:有 GaussDB / openGauss 就以它为准
    if re.search(r"(?i)opengauss", s):
        fam = "openGauss"
    elif re.search(r"(?i)gaussdb", s):
        fam = "GaussDB 分布式" if re.search(r"(?i)dws|distributed|分布式", s) else "GaussDB 集中式"
    else:
        fam = kbversions.family(s) if re.search(r"(?i)postgres", s) else ""
    m = re.search(r"(?i)(?:GaussDB(?:\s*Kernel)?|openGauss)\s*V?(\d+(?:\.\d+)+)", s or "")
    if not m:
        m = re.search(r"(?i)(?:GaussDB|openGauss)[^\d]{0,20}(\d+(?:\.\d+)+)", s or "")
    return fam, (m.group(1) if m else "")


def cmd_profile(args: argparse.Namespace) -> int:
    kb = _kb(args)
    system = re.sub(r"[^A-Za-z0-9_]", "_", args.system).upper()
    fam, ver = parse_version_string(args.version_string or "")
    meta: Dict[str, Any] = {"id": f"EP-{system}", "type": "profile", "title": f"{args.system} 环境与业务画像",
                            "system": args.system, "aliases": list(args.alias or []),
                            "product": fam or "TODO(GaussDB 集中式 / GaussDB 分布式 / openGauss)",
                            "version": ver or "TODO(登录输出里的版本号,如 505.2.0)",
                            "deployment": args.deployment or "TODO(如 一主两备)", "as_of": datetime.date.today().isoformat()}
    body = "\n".join([
        "## 摘要", f"{args.system}:{fam or '?'} {ver or '?'},{args.deployment or '?'}。<一句话:系统做什么、什么时候最忙>", "",
        "## 部署拓扑", "<主备 / 分布式节点数、同城异地,不写 IP>", "",
        "## 连接池", "<应用连接池种类与上限、超时、是否 removeAbandoned>", "",
        "## 批量窗口", "<如 22:00–02:00 日终批量;月末 / 季末加批>", "",
        "## 关键业务依赖", "<核心交易、上下游系统>", "",
        "## 高峰时段", "<如 工作日 9:30–11:30>", ""])
    out = kb / DRAFT_DIR / f"EP-{system}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(compose(meta, body), encoding="utf-8")
    print(f"画像草稿:{out.relative_to(kb)}")
    print("让管理员补全 TODO 与 <…>(只写连接别名,不写 IP / 主机名 / 口令),确认后:"
          f"python3 kb.py add {out.relative_to(kb)} --move")
    return 0


def cmd_taxonomy(args: argparse.Namespace) -> int:
    kb = _kb(args)
    path = kb / "topics" / kbtopics.TAXONOMY_FILE
    if path.exists() and not args.force:
        print(f"{path.relative_to(kb)} 已存在(要恢复默认加 --force)")
        return 0
    topics = list(kbtopics.DEFAULT_TOPICS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(kbtopics.taxonomy_yaml(topics), encoding="utf-8")
    print(f"已写出专题定义:{path.relative_to(kb)}(改完运行 kb.py index 重新生成 L0 / L1)")
    return 0


# ---------------------------------------------------------------- inbox-clean

_KEEP_INBOX = ("uploads", "drafts")


def clean_inbox(kb: pathlib.Path, slug: str, discard: bool = False) -> str:
    """只删 inbox/ 下一层的批次目录:规范批次(source.md / outline.md)直接删;工单批次要 items/ 已清空
    (apply 会把处理完的单子移走),还有没处理的单子就拒绝——删了就是丢原文。收件目录与草稿目录不删。"""
    root = (kb / "inbox").resolve()
    target = (kb / "inbox" / slug).resolve()
    if target.parent != root or slug in _KEEP_INBOX or not target.is_dir():
        raise ItemCmdError(f"inbox/{slug} 不是可清理的批次目录(只认 inbox/ 下一层,uploads / drafts 不删)")
    items = list((target / "items").glob("*.md")) if (target / "items").is_dir() else []
    if items and not discard:
        raise ItemCmdError(f"inbox/{slug} 还有 {len(items)} 单工单没处理(propose → review → apply 之后再清;"
                           f"确认这批导错了要整批丢弃,加 --discard,原件快照仍在 sources/)")
    # 官方文档集的批次(docset scan 产生):有 scan.json;apply 之后配方已复制进 docsets/<代号>/plan/,解包的 CHM 可以删
    if not any((target / f).exists() for f in ("source.md", "outline.md", "manifest.json", "scan.json")):
        raise ItemCmdError(f"inbox/{slug} 不像 ingest 产生的批次目录(没有 source.md / manifest.json / scan.json),不删")
    shutil.rmtree(target)
    return f"inbox/{slug}"


def cmd_inbox_clean(args: argparse.Namespace) -> int:
    kb = _kb(args)
    bad = 0
    with kblock.hold(kb):
        for slug in args.slugs:
            try:
                print(f"[已清理] {clean_inbox(kb, slug.strip().strip('/').split('/')[-1], args.discard)}")
            except ItemCmdError as exc:
                bad += 1
                print(f"[跳过 ] {exc}")
    return 2 if bad else 0


# ---------------------------------------------------------------- wiring

def add_subcommands(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("add", help="类型化条目草稿入库(校验 + 分配 ID + 放进类型目录)")
    p.add_argument("files", nargs="+", help="草稿 md,或放草稿的目录")
    p.add_argument("--kb")
    p.add_argument("--replace", action="store_true", help="同 ID 已存在时覆盖(更新一条)")
    p.add_argument("--move", action="store_true", help="入库成功后删掉草稿")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("attach", help="原始证据(照片、日志、报告)存进 evidence/")
    p.add_argument("files", nargs="+")
    p.add_argument("--kb")
    p.add_argument("--to", help="顺手登记到这条条目的 evidence")
    p.set_defaults(func=cmd_attach)
    p = sub.add_parser("retype", help="改类型:换目录与 ID,旧 ID 记进 aliases")
    p.add_argument("id")
    p.add_argument("--to", required=True, choices=sorted(kbtypes.TYPED))
    p.add_argument("--domain", help="改成手册时的 ID 域(如 LCK);不给就按专题取")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_retype)
    p = sub.add_parser("migrate", help="存量迁移:旧指南分类型、条款 / 案例补专题(先出清单,确认后 --apply)")
    p.add_argument("--kb")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--plan", help=f"清单路径(默认 <kb>/{PLAN_FILE})")
    p.set_defaults(func=cmd_migrate)
    p = sub.add_parser("profile", help="起草一份环境画像(管理员补全后 kb.py add)")
    p.add_argument("--system", required=True, help="系统简称,如 CBST")
    p.add_argument("--alias", action="append", help="连接别名(可多次),诊断时靠它匹配本实例")
    p.add_argument("--version-string", help="登录输出里的版本串(SELECT version() 的结果)")
    p.add_argument("--deployment", help="部署形态,如 一主两备")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_profile)
    p = sub.add_parser("inbox-clean", help="删掉处理完的 inbox/<批次>/(规范批次条款化完、工单批次全部 apply 后)")
    p.add_argument("slugs", nargs="+", help="批次名(inbox/ 下的目录名)")
    p.add_argument("--discard", action="store_true", help="整批丢弃导错的批次(还有没处理的工单也删;原件快照仍在 sources/)")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_inbox_clean)
    p = sub.add_parser("taxonomy", help="写出可编辑的专题定义 topics/_taxonomy.yaml")
    p.add_argument("--init", action="store_true", required=True)
    p.add_argument("--force", action="store_true", help="已存在时用默认覆盖")
    p.add_argument("--kb")
    p.set_defaults(func=cmd_taxonomy)
