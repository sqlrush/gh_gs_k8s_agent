"""kb.py 的引用核对子命令:cite-check——一段回答里引用的案例 ID / 条款 ID 是否真的在知识库里。

只报告不拦截:给用户复核模型的回答,也给模型交稿前自查。
  存在 / 前缀匹配   ID 在库里(报告里案例 ID 常被截断显示成 S2-20250120-CBST-…,去掉省略号后按前缀找)
  已废止           条款在 archive/,不得作为判定依据
  不唯一           截断的前缀对上了多个案例
  未找到           库里没有——按编造处理,退出码 2
等待事件名、GUC 名、错误码不是知识库 ID,不在核对范围。真相是 <kb>/ 的文件,不连库。

2026-10-24 八类知识:手册 RB- / 缺陷 KD- / 基线 BL- / 产品文档 PD- / Wiki WK- / 画像 EP- 也核对;
改过类型的旧 ID 记在新条目的 aliases 里,核对结果写「已改为 <新 ID>」。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Set, Tuple

from common.kb import cases as kbcases

CLAUSE_RE = re.compile(r"GS-[A-Z]{2,4}-\d{3}")
CASE_RE = re.compile(r"S[1-4]-\d{8}-[A-Za-z0-9_]+-[^\s,，;;:：、。!?！?()（）\[\]【】〔〕《》〈〉\"'`<>*]*")
_ELLIPSIS_RE = re.compile(r"(?:…|\.{2,})+$")
TYPED_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:WK-\d{4}|PD-[A-Z][A-Z0-9]{1,9}-\d{5}|PD-\d{4}|KD-\d{4}|RB-[A-Z]{2,4}-\d{3}"
                      r"|BL-[A-Z0-9_]+-[A-Z0-9_]+|EP-[A-Z0-9_]+)(?![A-Za-z0-9_])")

STATUS_MARK = {"存在": "✓", "前缀匹配": "✓", "已废止": "⚠", "不唯一": "⚠", "未找到": "✗"}


class CiteCmdError(Exception):
    pass


@dataclass(frozen=True)
class Cite:
    token: str
    kind: str          # case | clause | typed
    status: str        # 存在 | 前缀匹配 | 已废止 | 不唯一 | 未找到
    resolved: str = ""
    note: str = ""


def extract_ids(text: str) -> List[Tuple[str, str]]:
    """按出现顺序返回 (kind, token),同一 token 只算一次。"""
    found = [(m.start(), "case", m.group(0)) for m in CASE_RE.finditer(text)]
    found += [(m.start(), "clause", m.group(0)) for m in CLAUSE_RE.finditer(text)]
    found += [(m.start(), "typed", m.group(0)) for m in TYPED_RE.finditer(text)]
    out: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for _, kind, tok in sorted(found):
        if tok not in seen:
            seen.add(tok)
            out.append((kind, tok))
    return out


def known_ids(kb: pathlib.Path) -> Tuple[List[str], Set[str], Set[str]]:
    """(案例 ID 列表, 生效条款 ID, 已废止条款 ID)——全部来自文件。"""
    import kb as kbmain              # 同目录 kb.py:条款文件读取与 status 判定
    cases, _ = kbcases.load_cases(kb)
    case_ids = sorted(c.id for c in cases)
    active: Set[str] = set()
    archived: Set[str] = set()
    for sub in ("rules", "archive"):
        for path in kbmain.iter_files(kb, sub, (".yaml", ".yml")):
            entries, _ = kbmain.load_rule_file(path)
            for e in entries or []:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                if sub == "rules" and kbmain.rule_status(e) == kbmain.STATUS_ACTIVE:
                    active.add(str(e["id"]))
                else:
                    archived.add(str(e["id"]))
    return case_ids, active, archived


def typed_ids(kb: pathlib.Path) -> Dict[str, str]:
    """类型化条目:ID → 自己;改过类型的旧 ID → 新 ID。"""
    from common.kb import types as kbtypes
    items, _ = kbtypes.load_items(kb)
    out = {i.id: i.id for i in items}
    for i in items:
        for a in i.aliases:
            out.setdefault(a, i.id)
    return out


def _check_typed(tok: str, ids: Dict[str, str]) -> Cite:
    if tok not in ids:
        return Cite(tok, "typed", "未找到", note="疑似编造")
    if ids[tok] != tok:
        return Cite(tok, "typed", "前缀匹配", ids[tok], note=f"旧 ID,已改为 {ids[tok]}")
    return Cite(tok, "typed", "存在", tok)


def _check_case(tok: str, case_ids: List[str]) -> Cite:
    bare = _ELLIPSIS_RE.sub("", tok)
    if bare in case_ids:
        return Cite(tok, "case", "存在", bare)
    hits = [c for c in case_ids if c.startswith(bare)]
    if len(hits) == 1:
        return Cite(tok, "case", "前缀匹配", hits[0])
    if hits:
        return Cite(tok, "case", "不唯一", note="、".join(hits[:3]))
    return Cite(tok, "case", "未找到", note="疑似编造")


def _check_clause(tok: str, active: Set[str], archived: Set[str]) -> Cite:
    if tok in active:
        return Cite(tok, "clause", "存在", tok)
    if tok in archived:
        return Cite(tok, "clause", "已废止", tok, "条款在 archive/,不得作为判定依据")
    return Cite(tok, "clause", "未找到", note="疑似编造")


def check(text: str, kb: pathlib.Path) -> List[Cite]:
    case_ids, active, archived = known_ids(kb)
    typed = typed_ids(kb)
    out: List[Cite] = []
    for kind, tok in extract_ids(text):
        if kind == "case":
            out.append(_check_case(tok, case_ids))
        elif kind == "clause":
            out.append(_check_clause(tok, active, archived))
        else:
            out.append(_check_typed(tok, typed))
    return out


def render(cites: List[Cite]) -> str:
    if not cites:
        return "引用核对 : 文本里没有案例 ID / 条款 ID(也没有手册 / 缺陷 / 基线 / 产品文档 / Wiki / 画像的 ID)"
    lines = ["引用核对 :"]
    for c in cites:
        extra = f" → {c.resolved}" if c.status == "前缀匹配" else ""
        note = f"({c.note})" if c.note else ""
        lines.append(f"  {STATUS_MARK[c.status]} {c.token}  {c.status}{extra} {note}".rstrip())
    n_ok = sum(1 for c in cites if c.status in ("存在", "前缀匹配"))
    n_warn = sum(1 for c in cites if c.status in ("已废止", "不唯一"))
    n_bad = sum(1 for c in cites if c.status == "未找到")
    tail = "——未找到的按编造处理,回答里不得引用" if n_bad else ""
    lines.append(f"合计 {len(cites)} 个 ID:{n_ok} 个在库,{n_warn} 个需注意,{n_bad} 个未找到{tail}")
    return "\n".join(lines)


def _read_input(args: argparse.Namespace) -> str:
    if args.text:
        return str(args.text)
    if args.file:
        path = pathlib.Path(args.file).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CiteCmdError(f"读不了 {path}:{exc}")
    return sys.stdin.read()


def cmd_cite_check(args: argparse.Namespace) -> int:
    from common.kb import config as kbconfig
    kb = kbconfig.resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise CiteCmdError(f"KB 目录不存在:{kb}")
    text = _read_input(args)
    if not text.strip():
        raise CiteCmdError("没有可核对的文本(--text / --file / stdin)")
    cites = check(text, kb)
    if args.json:
        print(json.dumps([asdict(c) for c in cites], ensure_ascii=False, indent=2))
    else:
        print(render(cites))
    return 2 if any(c.status == "未找到" for c in cites) else 0


def add_subcommands(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("cite-check", help="核对回答里引用的案例 ID / 条款 ID 是否在库里(未找到 = 疑似编造,退出 2)")
    p.add_argument("--kb")
    p.add_argument("--text", help="要核对的文本")
    p.add_argument("--file", help="要核对的文件(不给 --text/--file 则读 stdin)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cite_check)
