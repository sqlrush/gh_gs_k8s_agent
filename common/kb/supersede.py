"""处置的「取代」:同一个方案、策略变了(参数改值、做法调整),新的处置取代旧的处置。

真相在图文件里旧处置那条 handled_by 边上:valid_to(从这天起不再是现行做法)+ superseded_by(被哪个案例取代)。
旧案例文件不改——它记的是当时真实发生的事,报告里可能引用过它的 ID。取代只让旧处置退出「现行做法」:
路径查询(valid_to 已到)不再给它,历史相似 / 专题页把它标「已被取代」并排到后面。

「是不是同一个方案」是语义判断,由模型在候选的 judgments 里逐条写(取代 / 并存 + 理由 + 原文摘录);
脚本做确定性的部分:列出要判断的旧处置、漏判就拦、用户在选择列表确认后才落盘。
谁取代谁按日期定(晚的取代早的),不靠导入顺序——补导历史工单时,旧工单被已有的新做法取代,方向也对。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import yaml

from . import graphfiles as gf
from . import text as kbtext
from .atomic import write_text_atomic

SUPERSEDE = "取代"
COEXIST = "并存"
VERDICTS = (SUPERSEDE, COEXIST)
WORK_ORDER_LIMIT = 40          # 工作单里最多列多少条现行处置;多了按与工单原文的词重合挑(review 仍按全量核对)


@dataclass(frozen=True)
class Handling:
    """一条处置:某案例(库里)或本批某工单,在某根因下做了什么。"""
    ref: str                   # 库里的:案例 ID;本批的:工单 id
    rootcause_id: str
    rootcause: str
    action_id: str
    action: str
    on: str = ""               # 发生日期 YYYY-MM-DD,未知为空
    seq: int = -1              # 本批里的候选序号;库里的为 -1
    in_batch: bool = False

    def label(self, names: Optional[Mapping[str, str]] = None) -> str:
        """names:本批工单 id → 将要生成的案例 ID(清单里给用户看案例 ID,不看工单号)。"""
        ref = (names or {}).get(self.ref, self.ref) if self.in_batch else self.ref
        return f"「{self.action}」({ref}" + (f",{self.on}" if self.on else "") + ")"

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Judgment:
    prior: Handling            # 模型拿来比的那条旧处置
    mine: Handling             # 本单的处置
    verdict: str
    reason: str
    quote: str

    def ordered(self) -> Tuple[Handling, Handling]:
        """(早的, 晚的):取代时晚的取代早的。"""
        return (self.mine, self.prior) if _before(self.mine, self.prior) else (self.prior, self.mine)


def _today() -> str:
    return datetime.date.today().isoformat()


def _before(a: Handling, b: Handling) -> bool:
    """a 是否早于 b:两边都有日期且不同按日期;否则库里的早于本批的、本批里按候选顺序。"""
    if a.on and b.on and a.on != b.on:
        return a.on < b.on
    if a.in_batch != b.in_batch:
        return not a.in_batch
    return a.seq < b.seq


def is_current(t: gf.Triple, today: Optional[str] = None) -> bool:
    """现行:已确认、没到失效日。与路径查询(store_files._edge_valid)同一口径。"""
    return t.in_path and (not t.valid_to or t.valid_to > (today or _today()))


def current_handlings(triples: Sequence[gf.Triple], today: Optional[str] = None) -> List[Handling]:
    """库里现行的 根因→处置(来自案例的 handled_by 边)。"""
    day = today or _today()
    return [Handling(ref=t.case_id, rootcause_id=t.src.id, rootcause=t.src.name,
                     action_id=t.dst.id, action=t.dst.name, on=t.valid_from)
            for t in triples
            if t.rel == "handled_by" and t.case_id and t.src.kind == "rootcause" and is_current(t, day)]


def for_work_order(handlings: Sequence[Handling], text: str, limit: int = WORK_ORDER_LIMIT) -> List[Dict[str, str]]:
    """工作单的 prior_handlings。不超过 limit 全给;超了按与工单原文的词重合挑(确定性,同分按根因 / 日期)。"""
    pool = list(handlings)
    if len(pool) > limit:
        have = set(kbtext.tokenize(text))
        pool = sorted(pool, key=lambda h: (-len(have & set(kbtext.tokenize(f"{h.rootcause} {h.action}"))),
                                           h.rootcause, h.on, h.ref))[:limit]
    return [{"case": h.ref, "date": h.on, "rootcause": h.rootcause, "action": h.action} for h in pool]


def edge_handling(ed: Any, ref: str, on: str, aliases: Mapping[str, str], seq: int = -1,
                  in_batch: bool = True) -> Optional[Handling]:
    """候选里的一条 handled_by 边 → Handling;不是 handled_by 或缺名字返回 None。"""
    if not isinstance(ed, dict) or ed.get("rel") != "handled_by":
        return None
    src, dst = ed.get("src") or {}, ed.get("dst") or {}
    if not isinstance(src, dict) or not isinstance(dst, dict) or not src.get("name") or not dst.get("name"):
        return None
    rname, aname = str(src["name"]), str(dst["name"])
    return Handling(ref=ref, rootcause_id=gf.canonical_id(str(src.get("kind") or "rootcause"), rname, dict(aliases)),
                    rootcause=rname, action_id=gf.canonical_id(str(dst.get("kind") or "action"), aname, dict(aliases)),
                    action=aname, on=on, seq=seq, in_batch=in_batch)


def batch_handlings(candidates: Sequence[Any], aliases: Mapping[str, str]) -> List[Handling]:
    """本批全部候选的处置(同批里早的工单也要被晚的判断)。"""
    out: List[Handling] = []
    for seq, cand in enumerate(candidates):
        if not isinstance(cand, dict) or not isinstance(cand.get("case"), dict):
            continue
        ref, on = str(cand.get("item_id") or ""), str(cand["case"].get("occurred_at") or "")
        out += [h for h in (edge_handling(ed, ref, on, aliases, seq) for ed in cand.get("edges") or []) if h]
    return out


def _matches(h: Handling, ref: str, action: str, aliases: Mapping[str, str]) -> bool:
    return h.ref == ref and (h.action_id == gf.canonical_id("action", action, dict(aliases))
                             or kbtext.normalize(h.action) == kbtext.normalize(action))


def check_judgments(cand: Mapping[str, Any], seq: int, text: str, priors: Sequence[Handling],
                    aliases: Mapping[str, str], quote_found: Callable[[str, str], bool],
                    extra_roots: Set[str] = frozenset()) -> Tuple[List[Judgment], List[str]]:
    """核对一单的 judgments。要判断的 = 同一根因(含待归一的相似根因)下、别的案例 / 本批更早工单的现行处置,
    处置一字不差的(同样的做法又用了一次)除外。返回 (判断, 错误);错误不带「候选 xxx:」前缀。"""
    item_id = str(cand.get("item_id") or "")
    on = str((cand.get("case") or {}).get("occurred_at") or "")
    mine = [h for h in (edge_handling(ed, item_id, on, aliases, seq) for ed in cand.get("edges") or []) if h]
    roots = {h.rootcause_id for h in mine} | set(extra_roots)
    actions = {h.action_id for h in mine}
    me = Handling(ref=item_id, rootcause_id="", rootcause="", action_id="", action="", on=on, seq=seq, in_batch=True)
    required = [h for h in priors
                if h.ref != item_id and h.rootcause_id in roots and h.action_id not in actions
                and (not h.in_batch or _before(h, me))]
    raw = cand.get("judgments") or []
    if not isinstance(raw, list):
        return [], ["judgments 必须是数组"]
    parsed = [_parse_judgment(j, item_id, mine, text, priors, aliases, quote_found) for j in raw]
    out = [j for j, _e in parsed if j is not None]
    errors = [e for _j, e in parsed if e]
    judged = {(j.prior.ref, j.prior.action_id) for j in out}
    for h in required:
        if (h.ref, h.action_id) not in judged:
            errors.append(f"根因「{h.rootcause}」本行已有处置{h.label()},没判断取代还是并存——"
                          f"在 judgments 里补一条(同一个方案、策略变了 → {SUPERSEDE};不同方案 → {COEXIST})")
    return out, errors


def _parse_judgment(j: Any, item_id: str, mine: Sequence[Handling], text: str, priors: Sequence[Handling],
                    aliases: Mapping[str, str], quote_found: Callable[[str, str], bool]
                    ) -> Tuple[Optional[Judgment], str]:
    """一条 judgment → (Judgment, "") 或 (None, 错误)。"""
    if not isinstance(j, dict):
        return None, "judgments 里每项必须是 {prior, action, verdict, reason, quote}"
    ref, action = str(j.get("prior") or "").strip(), str(j.get("action") or "").strip()
    verdict, reason = str(j.get("verdict") or "").strip(), str(j.get("reason") or "").strip()
    quote = str(j.get("quote") or "")
    prior = next((h for h in priors if h.ref != item_id and _matches(h, ref, action, aliases)), None)
    if prior is None:
        return None, (f"judgments 引用的旧处置「{action}」({ref})不存在——prior 写库里的案例 ID 或本批工单 id,"
                      "action 照抄工作单 prior_handlings 里的处置")
    if verdict not in VERDICTS:
        return None, f"judgments 对{prior.label()} 的 verdict 必须是 {'/'.join(VERDICTS)}"
    if not reason or reason.startswith("<"):
        return None, f"judgments 对{prior.label()} 没写理由"
    if not quote_found(quote, text):
        return None, f"judgments 对{prior.label()} 的摘录在原文里找不到(出处回指失败)"
    mine_h = next((h for h in mine if h.rootcause_id == prior.rootcause_id), mine[0] if mine else None)
    if mine_h is None:
        return None, f"judgments 对{prior.label()} 下了判断,但本单没有 handled_by 边"
    return Judgment(prior=prior, mine=mine_h, verdict=verdict, reason=reason, quote=quote), ""


def review_entry(n: int, item_id: str, case_id: str, j: Judgment,
                 names: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """选择列表的一项。取代没有默认(用户逐条定);并存只是告知,不改任何东西。"""
    names = {**(names or {}), item_id: case_id}
    if j.verdict == SUPERSEDE:
        early, late = j.ordered()
        text = f"[取代] {late.label(names)} 取代 {early.label(names)} · 理由:{j.reason}   ⚠ 无默认"
        return {"n": n, "item_id": item_id, "kind": "supersede", "case_id": case_id, "text": text,
                "quote": j.quote, "default": "ask", "payload": {"early": early.as_dict(), "late": late.as_dict(),
                                                                 "reason": j.reason}}
    return {"n": n, "item_id": item_id, "kind": "coexist", "case_id": case_id,
            "text": f"[并存] 与{j.prior.label(names)} 是不同方案,都保留 · 理由:{j.reason}",
            "quote": j.quote, "default": "accept", "payload": {"prior": j.prior.as_dict(), "reason": j.reason}}


# ---------------------------------------------------------------- 落盘(apply)

Mark = Tuple[str, str]           # (案例 ID, 处置节点 id)


def plan_marks(payloads: Sequence[Mapping[str, Any]], item2cid: Mapping[str, str], written: Sequence[str],
               accepted_actions: Mapping[str, Set[str]], today: Optional[str] = None
               ) -> Tuple[Dict[Mark, Tuple[str, str]], List[str]]:
    """用户接受的取代 → {(被取代的案例, 处置 id): (取代它的案例, 生效日)} 与跳过说明。
    取代要生效:晚的那单写入了、它的处置边被接受;早的在本批时也得写入了。"""
    marks: Dict[Mark, Tuple[str, str]] = {}
    skipped: List[str] = []
    for p in payloads:
        early, late = p["early"], p["late"]
        late_cid = item2cid.get(late["ref"]) if late.get("in_batch") else late["ref"]
        early_cid = item2cid.get(early["ref"]) if early.get("in_batch") else early["ref"]
        if late.get("in_batch") and (late_cid not in written or late["action_id"] not in accepted_actions.get(late_cid, set())):
            skipped.append(f"「{late['action']}」取代「{early['action']}」未生效:新的那单或它的处置边没被接受")
            continue
        if early.get("in_batch") and early_cid not in written:
            skipped.append(f"「{late['action']}」取代「{early['action']}」未生效:被取代的那单没写入")
            continue
        marks[(str(early_cid), str(early["action_id"]))] = (str(late_cid), late.get("on") or today or _today())
    return marks, skipped


def _row_key(raw: Any, aliases: Mapping[str, str]) -> Optional[Mark]:
    if not isinstance(raw, dict) or raw.get("rel") != "handled_by":
        return None
    dst = raw.get("dst") or {}
    if not isinstance(dst, dict) or not dst.get("name"):
        return None
    aid = str(dst.get("canonical") or "") or gf.canonical_id(str(dst.get("kind") or "action"), str(dst["name"]), dict(aliases))
    return str(raw.get("case") or ""), aid


def mark_rows(rows: Sequence[Any], marks: Mapping[Mark, Tuple[str, str]], aliases: Mapping[str, str]
              ) -> Tuple[List[Any], Set[Mark]]:
    """返回 (新行列表, 命中的 key)。已标过 superseded_by 的不再改(第一次取代为准)。"""
    out: List[Any] = []
    hit: Set[Mark] = set()
    for raw in rows:
        key = _row_key(raw, aliases)
        if key in marks and not raw.get("superseded_by"):
            by, on = marks[key]
            out.append({**raw, "valid_to": on, "superseded_by": by})
            hit.add(key)
        else:
            out.append(raw)
    return out, hit


def mark_in_files(kb: pathlib.Path, marks: Mapping[Mark, Tuple[str, str]], aliases: Mapping[str, str]
                  ) -> Tuple[List[str], List[Mark]]:
    """在 graph/*.yaml 里给被取代的旧处置边标 valid_to / superseded_by。返回 (改过的文件, 没找到的)。"""
    if not marks:
        return [], []
    changed: List[str] = []
    found: Set[Mark] = set()
    for path in gf.iter_graph_files(kb):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            continue
        rows, hit = mark_rows(data, marks, aliases)
        if hit:
            write_text_atomic(path, yaml.safe_dump(rows, allow_unicode=True, sort_keys=False))
            changed.append(str(path.relative_to(kb)))
            found |= hit
    return changed, [k for k in marks if k not in found]


# ---------------------------------------------------------------- 展示(索引 / 检索 / 专题页)

def superseded_info(triples: Sequence[gf.Triple], today: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """案例 ID → {"superseded": [{action, by, on}], "current": 现行处置条数};只收有处置被取代的案例。"""
    day = today or _today()
    gone: Dict[str, List[Dict[str, str]]] = {}
    live: Dict[str, int] = {}
    for t in triples:
        if t.rel != "handled_by" or not t.case_id or not t.in_path:
            continue
        if t.superseded_by and t.valid_to and t.valid_to <= day:
            gone.setdefault(t.case_id, []).append({"action": t.dst.name, "by": t.superseded_by, "on": t.valid_to})
        elif not t.valid_to or t.valid_to > day:
            live[t.case_id] = live.get(t.case_id, 0) + 1
    return {cid: {"superseded": items, "current": live.get(cid, 0)} for cid, items in gone.items()}


def fully_superseded(meta: Mapping[str, Any]) -> bool:
    """案例的处置全部被取代(没有现行处置)——检索与专题页把它排到后面。"""
    return bool(meta.get("superseded")) and not meta.get("current")


def note(items: Sequence[Mapping[str, str]]) -> str:
    return ";".join(f"处置「{i.get('action')}」已被 {i.get('by')} 取代({i.get('on')})" for i in items)


def info_hash(info: Mapping[str, Any]) -> str:
    """并进案例文档的 content_hash:取代发生在别的文件里,案例文件没变,增量索引也得重写它。"""
    return hashlib.sha256(json.dumps(info, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------- validate

LOOKALIKE_RATIO = 0.72           # 与 review 的 [归一] 同一门槛


def graph_findings(triples: Sequence[gf.Triple], case_ids: Set[str]) -> List[Tuple[str, str]]:
    """validate 用:superseded_by 的合法性,以及没归一的近似现象 / 根因(同一个东西两种写法,两边互相看不到)。"""
    out: List[Tuple[str, str]] = []
    for t in triples:
        if not t.superseded_by:
            continue
        where = f"{t.file}: {t.src.name} -{t.rel}-> {t.dst.name}"
        if t.rel != "handled_by":
            out.append(("error", f"{where} 写了 superseded_by——只有 handled_by(处置)边能被取代"))
        elif t.superseded_by not in case_ids:
            out.append(("error", f"{where} 的 superseded_by `{t.superseded_by}` 不是库里的案例"))
        elif not t.valid_to:
            out.append(("warn", f"{where} 写了 superseded_by 却没写 valid_to——这条处置仍算现行做法"))
    return out + _lookalike_nodes(triples)


def _lookalike_nodes(triples: Sequence[gf.Triple]) -> List[Tuple[str, str]]:
    import difflib
    names: Dict[str, Tuple[str, str]] = {}
    for t in triples:
        if t.status == "rejected":
            continue
        for ref in (t.src, t.dst):
            if ref.kind in ("symptom", "rootcause"):
                names.setdefault(ref.id, (ref.kind, ref.name))
    ids = sorted(names)
    out: List[Tuple[str, str]] = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            (ka, na), (kb_, nb) = names[a], names[b]
            if ka != kb_:
                continue
            ratio = difflib.SequenceMatcher(None, kbtext.normalize(na), kbtext.normalize(nb)).ratio()
            if ratio >= LOOKALIKE_RATIO:
                word = "现象" if ka == "symptom" else "根因"
                out.append(("warn", f"graph: {word}「{na}」与「{nb}」很像(相似度 {ratio:.2f})——是同一个{word}就在"
                                    f" graph/canonical.yaml 归一(`{a}: [{nb}]`),否则两边的案例与处置互相看不到、取代也判不到"))
    return out
