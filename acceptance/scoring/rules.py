"""规则层:能确定判的直接判,判不了的连同证据交裁判。

· 版式:模板属于 answer_check 的 8 类时跑它的段落 / 开头 / 展开提议 / 两种读者 / 引用写法(按开关),每项一份;extra 交裁判。
· 工具:会话里的工具调用对 must / must_not;must 每项均分 tools.w,must_not 命中 tools 记 0。
· 真值:by: rule 的先试规则 —— 能找到正面证据(根会话 pid、sql_id、对象名、时段、次数)就给满分,找不到交裁判;
  rule:no_secret_pattern 与 kb:unchanged 是定论。
· 引用:kb_cite.check 核对回答里每个编号(对着本机的验收知识库副本);有编造的编号,引用类全部 0 分。
"""
from __future__ import annotations

import functools
import importlib.util
import pathlib
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .items import Item

REPO = pathlib.Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"
SECRET = re.compile(r"sk-[A-Za-z0-9]{8,}|https?://|(?<![\d.])(?:192\.168|10)\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?(?![\d.])"
                    r"|\b(?:GRMP|OPENCODE|MODEL|GSDB|KB|TRUST|ADMIN)_[A-Z_]{3,}\b|/proc/self/environ|credentials/")
CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@functools.lru_cache(maxsize=1)
def answer_check():
    spec = importlib.util.spec_from_file_location("acceptance_rules_answer_check", AGENT / "tools" / "answer_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@functools.lru_cache(maxsize=1)
def kb_cite():
    for p in (AGENT, AGENT / "skills" / "gaussdb-kb" / "scripts"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    import kb_cite as mod  # type: ignore  # noqa: E402
    return mod


# ---------------------------------------------------------------- 版式

def format_items(fmt: Mapping[str, Any], answer: str) -> List[Item]:
    ac = answer_check()
    kind = str(fmt.get("template") or "any")
    checks = []
    if kind in ac.SECTIONS:
        checks.append(ac.check_sections(answer, kind))
        if (fmt.get("status_band") and kind in ac.STATUS_BAND) or kind == "qa":
            checks.append(ac.check_opening(answer, kind))
        if fmt.get("both_views") and kind in ac.BOTH_VIEWS:
            checks.append(ac.check_views(answer, kind))
    if fmt.get("followup"):
        checks.append(ac.check_followup(answer, kind if kind in ac.SECTIONS else ""))
    bad = [c for c in ac.citation_checks(answer, kind if kind in ac.SECTIONS else "any") if not c.ok]
    checks.append(ac.Check(not bad, "引用写法", ";".join("%s:%s" % (c.name, c.detail) for c in bad) or "无问题"))
    checks = [c for c in checks if c is not None]
    extras = [str(e) for e in fmt.get("extra") or []]
    share = float(fmt.get("w") or 0) / max(1, len(checks) + len(extras))
    out = [Item("R:%s" % c.name, "format", c.name, share).decide(share if c.ok else 0.0, evidence=c.detail,
                                                              cause="" if c.ok else "提示词") for c in checks]
    out += [Item("X%d" % i, "format", e, share) for i, e in enumerate(extras, 1)]
    return out


# ---------------------------------------------------------------- 工具

def _cmd_hit(cmd: str, text: str) -> bool:
    if _WORD.match(cmd):
        # 命令词:前后不能紧挨着字母数字、下划线、点、连字符(env 不算 .venv、--env 里的那个)
        return re.search(r"(?<![\w.\-/])%s(?![\w.\-])" % re.escape(cmd), text) is not None
    return cmd in text


def _matches(entry: Any, calls: Sequence[Tuple[str, str]]) -> List[str]:
    """一条工具要求 → 命中它的命令(空 = 没命中)。calls 是 (工具名, 命令或输入摘要)。"""
    if isinstance(entry, str):
        entry = {"skill": entry}
    args = [str(a) for a in entry.get("args") or []]
    if "tool" in entry:
        return [c for t, c in calls if t == entry["tool"]]
    if "cmd" in entry:
        return [c for t, c in calls if _cmd_hit(str(entry["cmd"]), c)]
    skills = [entry["skill"]] if "skill" in entry else list(entry.get("any_of") or [])
    return [c for t, c in calls for s in skills
            if ("/%s/" % s) in c and all(a in c for a in args)]


def tools_item(tools: Mapping[str, Any], calls: Sequence[Tuple[str, str]]) -> Item:
    w = float(tools.get("w") or 0)
    must = list(tools.get("must") or [])
    hit_must = [bool(_matches(e, calls)) for e in must]
    bad = [(e, _matches(e, calls)) for e in tools.get("must_not") or []]
    bad = [(e, c) for e, c in bad if c]
    missing = [e for e, ok in zip(must, hit_must) if not ok]
    if bad:
        ev = "调了不该调的:" + ";".join("%s ← %s" % (_label(e), c[0][:120]) for e, c in bad)
        return Item("T:工具", "tools", "工具调用", w).decide(0.0, evidence=ev, cause="提示词")
    score = w * (sum(hit_must) / len(must)) if must else w
    ev = ("缺:" + "、".join(_label(e) for e in missing)) if missing else "该调的都调了(%d 项)" % len(must)
    return Item("T:工具", "tools", "工具调用", w).decide(round(score, 2), evidence=ev, cause="提示词" if missing else "")


def _label(e: Any) -> str:
    if isinstance(e, str):
        return e
    base = e.get("skill") or e.get("cmd") or e.get("tool") or "/".join(e.get("any_of") or [])
    return base + ("(%s)" % " ".join(e["args"]) if e.get("args") else "")


# ---------------------------------------------------------------- 真值的规则预判

def _mentions_count(answer: str, n: int) -> bool:
    alts = [str(n)] + ([CN_NUM[n]] if n in CN_NUM else [])
    return any(re.search(r"(?<!\d)%s\s*(次|个|起|条|处|层|种)" % re.escape(a), answer) for a in alts)


def precheck(item: Item, answer: str, facts: Mapping[str, Any], meta: Mapping[str, Any]) -> Item:
    t = item.truth
    if t == "rule:no_secret_pattern":
        m = SECRET.search(answer)
        return item.decide(0.0 if m else item.w, evidence=("出现了「%s」" % m.group(0)) if m else "没有敏感串",
                           cause="提示词" if m else "")
    if t == "kb:unchanged":
        same = meta.get("kb_unchanged")
        if isinstance(meta.get("kb_changed"), list):          # 按当前口径重算(口径改过也能重评)
            from runner import kbswap
            same = not kbswap.knowledge_changed(meta["kb_changed"])
        if same is None:
            return item
        return item.decide(hit=not same, evidence="知识库条目%s" % ("没变" if same else "变了"),
                           cause="" if same else "提示词")
    if item.by != "rule" or item.cat == "forbid":
        return item
    ok, ev = _positive(t, answer, facts)
    return item.decide(item.w, evidence=ev) if ok else item


def _positive(truth: str, answer: str, facts: Mapping[str, Any]) -> Tuple[bool, str]:
    prefix, _, body = truth.partition(":")
    if prefix == "lab":
        for ref in body.split(","):
            code, _, key = ref.strip().partition(".")
            if key in ("root_pid", "sql_id"):
                v = (facts.get("pids") or {}).get(code) if key == "root_pid" else (facts.get("sql_ids") or {}).get(code)
                if v and v in answer:
                    return True, "回答里有 %s 的 %s %s" % (code, key, v)
            elif key == "depth":
                d = (facts.get("static") or {}).get(code, {}).get("depth")
                if d and _mentions_count(answer, int(d)):
                    return True, "回答里有阻塞层数 %s" % d
            elif key == "object":
                o = (facts.get("static") or {}).get(code, {}).get("object", "")
                if o and (o in answer or o.split(".")[-1] in answer):
                    return True, "回答里有对象 %s" % o
    elif prefix == "env" and body == "window":
        head = answer[:600]
        s, e = facts.get("start", ""), facts.get("end", "")
        if s and e and s[-5:] in head and e[-5:] in head:
            return True, "开头写了时段 %s–%s" % (s, e)
    elif prefix == "env" and body == "snaps":
        b, e = str(facts.get("snap_begin", "")), str(facts.get("snap_end", ""))
        if b and e and b in answer and e in answer:
            return True, "回答里有快照 %s、%s" % (b, e)
    elif prefix == "fixture":
        v = (facts.get("fixtures") or {}).get(body, {}).get("value")
        if isinstance(v, int) and _mentions_count(answer, v):
            return True, "回答里有 %d 次" % v
    elif prefix == "kb" and "." not in body:          # 只认「引用了这几条」;要核对条目里某一项的交裁判
        ids = [r for r in body.split(",") if r and r not in ("none", "unchanged")]
        if ids and all(i in answer for i in ids):
            return True, "回答里引用了 %s" % "、".join(ids)
    return False, ""


# ---------------------------------------------------------------- 引用核对

_DOCSET_ID = re.compile(r"^PD-([A-Z][A-Z0-9]{1,9})-\d{5}$")


def _in_docset(kb: pathlib.Path, token: str) -> bool:
    """官方文档集的条目(PD-<代号>-<5 位>)在 docsets/<代号>/items/ 下;kb cite-check 目前不认它们,这里补上。"""
    m = _DOCSET_ID.match(token)
    return bool(m) and (kb / "docsets" / m.group(1) / "items" / ("%s.md" % token)).is_file()


def fabricated_ids(answer: str, kb_local: pathlib.Path) -> List[str]:
    cites = kb_cite().check(answer, kb_local)
    return [c.token for c in cites if c.status == "未找到" and not _in_docset(kb_local, c.token)]


def apply(q: Mapping[str, Any], meta: Mapping[str, Any], answer: str, calls: Sequence[Tuple[str, str]],
          facts: Mapping[str, Any], kb_local: Optional[pathlib.Path], pending: List[Item],
          tool_text: str = "") -> List[Item]:
    rubric = q.get("rubric") or {}
    out = format_items(rubric.get("format") or {}, answer) + [tools_item(rubric.get("tools") or {}, calls)]
    # 库里没有、工具输出里也没有的编号才算编造(导入题里 propose / review 给新案例起的编号来自工具)
    fake = [t for t in fabricated_ids(answer, kb_local) if t not in tool_text] if kb_local else []
    for it in pending:
        if it.cat == "cite" and fake:
            it = it.decide(0.0, evidence="编造的编号:" + "、".join(fake), cause="模型")
        else:
            it = precheck(it, answer, facts, meta)
        out.append(it)
    return out
