"""kb.py 的工单/案例侧子命令:ingest(工单)/ propose / review / apply。

分工:
  ingest   确定性——csv/xlsx/md/txt → inbox/<slug>/items/*.md(一单一文件),写 manifest;
  propose  确定性——出工作单 inbox/<slug>/work/NNN.json(原文 + 案例 schema + 已知实体),
           模型据此写 inbox/<slug>/candidates.json;首次导入该类材料只问转化策略、不出工作单
           (策略未经用户确认,模型没有可填的工作单——跳不过去);
  review   确定性——校验候选(schema / 出处回指 / 实体归一 / ID 唯一)→ 编号选择列表 review.md + review.json;
  apply    确定性——按 decisions(编号的接受/改/拒)写 cases/*.md、graph/<slug>.yaml、canonical.yaml。

写库之前唯一的闸门就是 review 出的选择列表:模型只提议,用户按编号定,脚本落盘。
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from common.kb import cases as kbcases
from common.kb import graphfiles as gf
from common.kb import ingest as kbingest
from common.kb import lock as kblock
from common.kb.atomic import write_text_atomic
from common.kb import store_graph as sg
from common.kb import text as kbtext

WORK_BATCH = 8
CANDIDATE_SCHEMA_VERSION = 1


class CaseCmdError(Exception):
    pass


def _kb(args) -> pathlib.Path:
    from common.kb import config as kbconfig
    kb = kbconfig.resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise CaseCmdError(f"KB 目录不存在:{kb}")
    return kb


def _slug(stem: str) -> str:
    s = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", stem).strip("-").lower()
    return s or "imported"


def _read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CaseCmdError(f"{path.name} 读取/解析失败:{exc}")


def _write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- ingest(工单)

def ingest_tickets(kb: pathlib.Path, src: pathlib.Path, slug: Optional[str], redact: bool) -> Tuple[pathlib.Path, int]:
    """返回 (inbox/<slug>, 条数)。表格按列猜映射;md/txt 按 --- 分单。"""
    suffix = src.suffix.lower()
    name = src.name
    if suffix == ".xlsx":
        headers, rows = kbingest.read_xlsx(src)
        mapping = kbingest.guess_columns(headers)
        items = kbingest.rows_to_items(headers, rows, mapping, name)
    elif suffix == ".csv":
        headers, rows = kbingest.read_csv(src)
        mapping = kbingest.guess_columns(headers)
        items = kbingest.rows_to_items(headers, rows, mapping, name)
    elif suffix in (".md", ".txt"):
        text = kbingest._decode(src.read_bytes())
        items = kbingest.split_text_items(text, name, src.stem)
        headers, mapping = [], {}
    elif suffix in (".docx", ".doc", ".pdf"):
        import kb as kbmain              # 同目录的 kb.py:docx/doc/pdf 提取与质量闸门
        text = kbmain.normalize_text(kbmain.extract_source(src))
        items = kbingest.split_text_items(text, name, src.stem)
        headers, mapping = [], {}
    else:
        raise CaseCmdError(f"不支持的工单格式 {suffix}(支持 .xlsx/.csv/.md/.txt/.docx/.doc/.pdf)")
    if not items:
        raise CaseCmdError(f"{name}:没有解析出任何工单")

    slug = slug or _slug(src.stem)
    inbox = kb / "inbox" / slug
    snapshot_dir = kb / "sources"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snap = snapshot_dir / name
    if snap.exists():
        n = 2
        while (snapshot_dir / f"{src.stem}.v{n}{src.suffix}").exists():
            n += 1
        snap = snapshot_dir / f"{src.stem}.v{n}{src.suffix}"
    shutil.copy2(src, snap)
    written = kbingest.write_items(inbox / "items", items, snap.name, do_redact=redact)
    manifest = {"source": snap.name, "kind": "tickets", "count": len(written),
                "headers": headers, "mapping": mapping, "redacted": redact,
                "items": [p.name for p in written]}
    _write_json(inbox / "manifest.json", manifest)
    return inbox, len(written)


def cmd_ingest_tickets(args: argparse.Namespace, kb: pathlib.Path) -> int:
    src = pathlib.Path(args.file).expanduser()
    if not src.is_file():
        raise CaseCmdError(f"文件不存在:{src}")
    inbox, n = ingest_tickets(kb, src, args.slug, args.redact)
    manifest = _read_json(inbox / "manifest.json")
    print(f"KB 目录      : {kb}")
    print(f"原始快照     : sources/{manifest['source']}")
    print(f"工单拆分     : {inbox.relative_to(kb)}/items/({n} 单,一单一文件)")
    if manifest["mapping"]:
        print("列映射(猜)   : " + " · ".join(f"{k}←{v}" for k, v in manifest["mapping"].items()))
        unmapped = [h for h in manifest["headers"] if h not in manifest["mapping"].values()]
        if unmapped:
            print(f"未映射列     : {'、'.join(unmapped)}(保留在「原始字段」里)")
    print("原文已进索引 : 下次 `kb.py index` 后即可按原始工单检索(kind=raw)")
    print(f"Next: python3 kb.py propose {inbox.name}")
    return 0


# ---------------------------------------------------------------- propose

CASE_FIELDS = ("title", "system", "instance", "occurred_at", "engine", "severity", "primary_factor",
               "secondary_factors", "conclusion", "objects", "signals", "rules")

CANDIDATE_TEMPLATE = {
    "schema": CANDIDATE_SCHEMA_VERSION,
    "item_id": "<工单 id,原样>",
    "case": {
        "title": "<一句话标题,≤40 字>", "system": "<业务系统>", "instance": "<实例,不知道写 未知>",
        "occurred_at": "<YYYY-MM-DD>", "engine": "<gaussdb|opengauss>", "severity": "<S1|S2|S3|S4>",
        "primary_factor": "<根因一句话>", "secondary_factors": ["<次要因素>"],
        "conclusion": "<已确认|推测|待验证 —— 原文明确写了根因且已验证才是已确认>",
        "objects": ["<表/GUC/等待事件/错误码,原样>"], "signals": ["<复发标志:再出现时能观察到什么>"],
        "rules": ["<引用的客户条款 ID,如 GS-VAC-002,没有留空>"],
        "topics": ["<所属专题 slug:conn/lock/sql/proc/wait/wal/mem/vacuum/repl/param/space/batch>"],
        "sections": {"现场": "<现象,含现场背景:系统、版本、时段、负载>", "判断": "<根因推理>", "处置": "<做了什么>",
                     "复发标志": "<再发时的信号>",
                     "候选原因": "<可选:当时怀疑过哪些原因>", "取证顺序": "<可选:先查什么、再查什么>",
                     "排除理由": "<可选:其他原因凭什么排除>", "最终验证": "<可选:怎么确认处置生效>",
                     "区分证据": "<同一现象本行有过别的根因时必填:凭什么判断是这个根因而不是那个>"},
    },
    "quotes": {"primary_factor": "<原文片段,必须逐字出现在工单里>", "处置": "<原文片段>", "现场": "<原文片段>"},
    "entities": [{"kind": "object|guc|wait_event|error|component", "name": "<原样>", "quote": "<原文片段>"}],
    "edges": [{
        "src": {"kind": "symptom", "name": "<现象>"}, "rel": "caused_by|handled_by|involves|references|depends_on",
        "dst": {"kind": "rootcause", "name": "<根因>"}, "quote": "<原文片段>", "confidence": 0.8,
    }],
}

# 写图/写向量之前要定的转化策略——问的是"这批材料怎么变成向量和图",不是元数据缺省值。
# 答案写进 strategies/tickets.yaml(key: 答案原文),之后每次 propose 都会把它塞进工作单约束模型抽取。
STRATEGY_QUESTIONS = [
    {"key": "target", "q": "这批材料沉淀成什么?",
     "options": ["案例(现象→根因→处置,进向量 + 图)", "规范条款(进 rules/,只进向量)", "两者都有(报告里的规范引用也抽成条款)"],
     "default": "案例"},
    {"key": "chain", "q": "图里每单抽几条因果链?",
     "options": ["一单一条主链(现象→根因→处置)", "允许多条(次要因素也建 caused_by 边,置信度打折)"],
     "default": "一单一条主链"},
    {"key": "relations", "q": "除因果链外还抽哪些关系进图?(可多选)",
     "options": ["案例→涉及对象(表 / GUC / 等待事件 / 错误码)", "案例→引用条款", "对象→对象依赖(只在材料明确写了时)"],
     "default": "涉及对象 + 引用条款"},
    {"key": "signals", "q": "复发标志(各 skill 按发现匹配案例靠它)从哪取?",
     "options": ["材料里的告警代码 / 等待事件 / GUC 名(优先)", "现象描述里的关键短语", "两者都要"],
     "default": "两者都要"},
    {"key": "merge", "q": "现象 / 根因与库里已有节点同义时?",
     "options": ["合并到已有节点(canonical),让多个案例支持同一条链", "各自新建"],
     "default": "合并"},
    {"key": "vector", "q": "哪些小节进向量?",
     "options": ["现场 / 判断 / 处置 / 复发标志全部", "只现场 + 复发标志(入口更准,但处置文本不可语义召回)"],
     "default": "全部"},
    {"key": "confidence", "q": "置信度口径?",
     "options": ["结论强度 已确认=1.0 / 推测=0.6 / 待验证=0.3;用户在选择列表接受的边=1.0;模型自报封顶 0.9"],
     "default": "按此口径"},
    {"key": "defaults", "q": "缺省元数据(材料没写时)?",
     "options": ["engine=gaussdb · severity=S3 · 系统名原样 · 对象名保留 schema 前缀"],
     "default": "按此缺省"},
]


def load_strategy(kb: pathlib.Path) -> Dict[str, str]:
    path = kb / "strategies" / "tickets.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def strategy_rules(strategy: Dict[str, str]) -> List[str]:
    """把策略答案翻成给模型的抽取约束(字符串匹配,答案是原文)。"""
    rules: List[str] = []
    if "条款" in strategy.get("target", "") and "案例" not in strategy.get("target", ""):
        rules.append("这批材料只沉淀成规范条款:不写 case/edges,把每条可判定要求写成候选条款")
    if "多条" in strategy.get("chain", ""):
        rules.append("允许多条因果链:次要因素各建一条 caused_by 边,confidence 比主链低 0.2")
    else:
        rules.append("每单只抽一条主链:现象 —caused_by→ 根因 —handled_by→ 处置;次要因素写进 secondary_factors,不建边")
    rel = strategy.get("relations", "")
    if rel and "对象" not in rel:
        rules.append("不抽 涉及对象(involves)关系")
    if rel and "条款" not in rel:
        rules.append("不抽 引用条款(references)关系")
    if "依赖" in rel:
        rules.append("材料明确写了对象依赖(A 依赖 B / A 下游是 B)时抽 depends_on 边,没写不猜")
    sig = strategy.get("signals", "")
    if "告警" in sig and "短语" not in sig:
        rules.append("signals 只取材料里的告警代码 / 等待事件 / GUC 名,不写现象短语")
    elif "短语" in sig and "告警" not in sig:
        rules.append("signals 取现象描述里的关键短语")
    else:
        rules.append("signals 同时取告警代码 / 等待事件 / GUC 名 和 现象关键短语,告警代码写在前面")
    if "新建" in strategy.get("merge", ""):
        rules.append("现象 / 根因不与已有节点合并,各自新建(known_entities 只作参考)")
    else:
        rules.append("现象 / 根因与 known_entities 里的同义节点合并:直接用已有的名字")
    if "只现场" in strategy.get("vector", ""):
        rules.append("向量只索引 现场 与 复发标志 两节(judge/处置 仍写进案例文件)")
    return rules


def write_default_strategy(kb: pathlib.Path) -> pathlib.Path:
    """用户说「全按默认」时由脚本代写 strategies/tickets.yaml(key: 默认答案),模型不用手写 yaml。"""
    path = kb / "strategies" / "tickets.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {q["key"]: q["default"] for q in STRATEGY_QUESTIONS}
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def print_strategy_questions() -> None:
    print("首次导入工单:写图/写向量之前先和用户确认转化策略——策略未确认,不出工作单。逐题问,每题带选项与默认:")
    for i, q in enumerate(STRATEGY_QUESTIONS, 1):
        print(f"  {i}. [{q['key']}] {q['q']}")
        for opt in q["options"]:
            print(f"       - {opt}")
        print(f"       默认:{q['default']}")


def _known_entities(kb: pathlib.Path, limit: int = 60) -> List[str]:
    aliases, _ = gf.load_canonical(kb)
    ids = sorted(set(aliases.values()))
    triples, _ = gf.load_triples(kb)
    for ref in gf.nodes_of(triples).values():
        if ref.kind in ("object", "guc", "wait_event", "error", "symptom", "rootcause", "action") and ref.id not in ids:
            ids.append(ref.id)
    return ids[:limit]


def cmd_propose(args: argparse.Namespace, kb: pathlib.Path) -> int:
    inbox = kb / "inbox" / args.slug
    items_dir = inbox / "items"
    if not items_dir.is_dir():
        raise CaseCmdError(f"inbox/{args.slug}/items 不存在(先 ingest)")
    items = sorted(items_dir.glob("*.md"))
    if not items:
        raise CaseCmdError(f"inbox/{args.slug}/items 里没有工单")
    done = set()
    cand_path = inbox / "candidates.json"
    if cand_path.is_file():
        done = {c.get("item_id") for c in _read_json(cand_path) if isinstance(c, dict)}
    pending = [p for p in items if p.stem not in done and _item_id(p) not in done]
    batch = pending[args.offset:args.offset + args.batch]
    strategy = load_strategy(kb)
    if not strategy and args.use_defaults:
        write_default_strategy(kb)
        strategy = load_strategy(kb)
        print("转化策略     : 用户选「全按默认」,已写入 strategies/tickets.yaml")
    if not strategy:                                   # 闸门:策略没经用户确认,不出工作单
        print_strategy_questions()
        print(f"Next: 逐题向用户确认 → 答案按 key 写进 strategies/tickets.yaml"
              f"(用户说全按默认:python3 kb.py propose {args.slug} --use-defaults)→ 重跑 propose")
        return 2
    unknown = sorted(set(strategy) - {q["key"] for q in STRATEGY_QUESTIONS})
    if unknown:
        print(f"[warn ] strategies/tickets.yaml 有不认识的 key:{'、'.join(unknown)}"
              f"(合法 key:{'、'.join(q['key'] for q in STRATEGY_QUESTIONS)})")
    work_dir = inbox / "work"
    if work_dir.is_dir():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    known = _known_entities(kb)
    base_rules = [
        "每个 quotes/entities/edges 的 quote 必须是原文里逐字出现的片段(review 会逐条核对,对不上的整项作废)",
        "拿不准的字段留空,不要编;conclusion 宁可写 推测",
        "objects/entities 用原文的叫法;known_entities 里有同一个东西就用它的名字",
        "edges 只写原文能支撑的 现象→根因(caused_by)、根因→处置(handled_by);confidence 是你的把握(0.5–0.9)",
    ]
    for n, path in enumerate(batch, 1):
        text = path.read_text(encoding="utf-8")
        settled = _settled_fields(path)
        rules = base_rules + strategy_rules(strategy)
        if settled:
            # 材料已经过 gaussdb-kb-init 标准化:时间 / 系统 / 级别 / 结论都定过了。让模型照抄,
            # 只做语义部分——重抽一遍会与标准化文档分叉,而没有任何东西会发现(review 现在会拦)。
            rules = rules + ["case 里的 %s 以 known 为准(材料已标准化过),照抄不要另起一套;"
                             "确实认为 known 写错了,先改标准化文档再重跑,不要在候选里偷偷改"
                             % "、".join(settled)]
        _write_json(work_dir / f"{n:03d}.json", {
            "item_id": _item_id(path), "file": str(path.relative_to(kb)), "text": text,
            "candidate_template": CANDIDATE_TEMPLATE, "known_entities": known,
            "known": settled,                           # 标准化材料的已定字段;普通材料为 {}
            "strategy": strategy,                       # 用户确认过的转化策略(首次为空:先确认再填候选)
            "rules": rules,
        })
    print(f"工作单       : {work_dir.relative_to(kb)}/(本批 {len(batch)} 单,剩余 {max(0, len(pending) - len(batch))} 单)")
    print(f"候选输出到   : {cand_path.relative_to(kb)}(JSON 数组,每单一个对象,形状见工作单 candidate_template)")
    print("转化策略     : " + " · ".join(f"{k}={v[:18]}" for k, v in strategy.items()))
    print(f"Next: 逐单阅读工作单填写候选 → python3 kb.py review {args.slug}")
    return 0 if batch else 2


# 标准化(gaussdb-kb-init)材料在 item frontmatter 里带这几个已定字段。判定「是否标准化过」
# 用 system + occurred_at 同时存在 —— 与 ingest 认 std 块的条件同一套,不另立一个标记。
_SETTLED_FIELDS = ("occurred_at", "system", "severity", "conclusion")


def _settled_fields(item_path: pathlib.Path) -> Dict[str, str]:
    """item frontmatter 里由标准化文档定下来的字段;不是标准化材料就返回 {}。"""
    try:
        meta, _, _ = kbcases.split_frontmatter(item_path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    meta = meta or {}
    if not (str(meta.get("system") or "").strip() and str(meta.get("occurred_at") or "").strip()):
        return {}
    return {k: str(meta[k]).strip() for k in _SETTLED_FIELDS if str(meta.get(k) or "").strip()}


def _item_id(path: pathlib.Path) -> str:
    try:
        meta, _, _ = kbcases.split_frontmatter(path.read_text(encoding="utf-8"))
        if meta and meta.get("item_id"):
            return str(meta["item_id"])
    except OSError:
        pass
    return path.stem


# ---------------------------------------------------------------- review

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", kbtext.normalize(s))


def quote_found(quote: str, text: str) -> bool:
    """出处回指:摘录去空白后必须是原文的子串(允许模型把换行/空格抹平)。"""
    q = _norm(quote or "")
    return bool(q) and q in _norm(text)


def case_id_for(case: Dict[str, Any], fallback_system: str = "UNK") -> str:
    sev = str(case.get("severity") or "S3")
    date = str(case.get("occurred_at") or "").replace("-", "")[:8] or "00000000"
    system = re.sub(r"[^A-Za-z0-9_]", "", str(case.get("system") or fallback_system)) or fallback_system
    title = re.sub(r"[\s/\\:*?\"<>|#]+", "", str(case.get("title") or "未命名"))[:24]
    return f"{sev}-{date}-{system}-{title}"


def _similar_entities(name: str, known: Sequence[str], aliases: Dict[str, str]) -> List[Tuple[str, float]]:
    key = kbtext.normalize(name)
    if key in aliases:
        return []
    out = []
    for cid in known:
        bare = cid.split(":", 1)[1] if ":" in cid else cid
        ratio = difflib.SequenceMatcher(None, key, kbtext.normalize(bare)).ratio()
        if ratio >= 0.72 and ratio < 1.0:
            out.append((cid, round(ratio, 2)))
    return sorted(out, key=lambda p: -p[1])[:2]


def review_candidates(kb: pathlib.Path, slug: str, candidates: List[Dict[str, Any]]
                      ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """返回 (编号条目列表, 错误列表)。条目:{n, item_id, kind, text, default, payload}。"""
    inbox = kb / "inbox" / slug
    existing_ids = {c.id for c in kbcases.load_cases(kb)[0]}
    aliases, _ = gf.load_canonical(kb)
    known = _known_entities(kb, limit=500)
    confirmed_causes = _confirmed_causes(kb)
    entries: List[Dict[str, Any]] = []
    errors: List[str] = []
    n = 0
    seen_ids = set()

    for ci, cand in enumerate(candidates):
        if not isinstance(cand, dict) or not isinstance(cand.get("case"), dict):
            errors.append(f"候选 #{ci}:不是 {{item_id, case, quotes, entities, edges}} 形状")
            continue
        item_id = str(cand.get("item_id") or "")
        item_path = next((p for p in (inbox / "items").glob("*.md") if _item_id(p) == item_id), None)
        if item_path is None:
            errors.append(f"候选 {item_id!r}:inbox/{slug}/items 里没有这单")
            continue
        text = item_path.read_text(encoding="utf-8")
        case = cand["case"]
        cid = case_id_for(case)
        if cid in existing_ids or cid in seen_ids:
            errors.append(f"候选 {item_id}:案例 ID {cid} 已存在(同一天同系统同标题?改标题或确认是重复工单)")
            continue
        seen_ids.add(cid)
        sections = case.get("sections") or {}
        for s in kbcases.REQUIRED_SECTIONS:
            if not str(sections.get(s) or "").strip():
                errors.append(f"候选 {item_id}:小节「{s}」为空")
        if str(case.get("conclusion") or "") not in kbcases.CONCLUSION_CONFIDENCE:
            errors.append(f"候选 {item_id}:conclusion 必须是 已确认/推测/待验证")
        # 材料经 gaussdb-kb-init 标准化过的:候选与标准化文档说法不一,当场点名。
        # 静默采用候选那一版,知识库与交给客户的标准化文档就此分叉,而两边都还「看起来正常」。
        for fld, settled in _settled_fields(item_path).items():
            got = str(case.get(fld) or "").strip()
            if got and got != settled:
                errors.append(
                    f"候选 {item_id}:{fld} 与标准化文档不一致(标准化 {settled} / 候选 {got})"
                    f"——两者必须一致:改候选照抄,或先改标准化文档(kb_init render)再重跑")
        quotes = cand.get("quotes") or {}
        if not str(quotes.get("现场") or "").strip():
            errors.append(f"候选 {item_id}:quotes.现场 缺失——「现场」小节必须附原文摘录")
        if str(case.get("conclusion") or "") == "已确认" and not str(quotes.get("primary_factor") or "").strip():
            errors.append(f"候选 {item_id}:结论强度 已确认 但 quotes.primary_factor 没有原文摘录——补摘录,或把结论改成 推测")
        bad_quotes = [k for k, q in quotes.items() if not quote_found(str(q), text)]
        for k in bad_quotes:
            errors.append(f"候选 {item_id}:字段「{k}」的摘录在原文里找不到(出处回指失败)")
        if errors and any(e.startswith(f"候选 {item_id}") for e in errors):
            continue

        n += 1
        topics = [str(x) for x in (case.get("topics") or []) if str(x).strip() and not str(x).startswith("<")]
        look = _lookalike_note(cand, sections, confirmed_causes)
        entries.append({"n": n, "item_id": item_id, "kind": "case", "case_id": cid,
                        "text": f"[案例] {cid} · 结论强度 {case.get('conclusion')} · 级别 {case.get('severity') or '?'}"
                                f" · 专题 {'、'.join(topics) or '—(请补)'}" + look,
                        "quote": quotes.get("primary_factor") or quotes.get("现场") or "", "default": "accept",
                        "payload": {"case": case, "item_file": str(item_path.relative_to(kb)),
                                    "source": _source_of(item_path)}})
        for fld in ("severity", "conclusion", "primary_factor"):
            if case.get(fld):
                n += 1
                entries.append({"n": n, "item_id": item_id, "kind": "field", "case_id": cid, "field": fld,
                                "text": f"[字段] {fld} = {case[fld]}", "quote": quotes.get(fld, ""),
                                "default": "accept", "payload": {"field": fld, "value": case[fld]}})
        for ent in cand.get("entities") or []:
            if not isinstance(ent, dict) or not ent.get("name"):
                continue
            kind = str(ent.get("kind") or "object")
            if kind not in sg.NODE_KINDS:
                errors.append(f"候选 {item_id}:实体 kind {kind!r} 非法")
                continue
            q_ok = quote_found(str(ent.get("quote") or ""), text)
            n += 1
            entries.append({"n": n, "item_id": item_id, "kind": "entity", "case_id": cid,
                            "text": f"[实体] {kind} {ent['name']}" + ("" if q_ok else " ⚠ 摘录未回指"),
                            "quote": ent.get("quote", ""), "default": "accept" if q_ok else "reject",
                            "payload": {"kind": kind, "name": ent["name"]}})
            for cid2, ratio in _similar_entities(str(ent["name"]), known, aliases):
                n += 1
                entries.append({"n": n, "item_id": item_id, "kind": "merge", "case_id": cid,
                                "text": f"[归一] 「{ent['name']}」≈ {cid2} ? 相似度 {ratio}",
                                "quote": "", "default": "ask",
                                "payload": {"name": ent["name"], "kind": kind, "into": cid2}})
        for ed in cand.get("edges") or []:
            if not isinstance(ed, dict):
                continue
            src, dst = ed.get("src") or {}, ed.get("dst") or {}
            rel = str(ed.get("rel") or "")
            if rel not in sg.REL_TYPES or not src.get("name") or not dst.get("name"):
                errors.append(f"候选 {item_id}:边 {src.get('name')} -{rel}-> {dst.get('name')} 不合法")
                continue
            q_ok = quote_found(str(ed.get("quote") or ""), text)
            n += 1
            entries.append({"n": n, "item_id": item_id, "kind": "edge", "case_id": cid,
                            "text": f"[边] {src['name']} —{rel.upper()}→ {dst['name']}" + ("" if q_ok else " ⚠ 摘录未回指") + "   ⚠ 无默认",
                            "quote": ed.get("quote", ""), "default": "ask",
                            "payload": {"src": src, "rel": rel, "dst": dst,
                                        "confidence": float(ed.get("confidence") or 0.7)}})
    return entries, errors


def _confirmed_causes(kb: pathlib.Path) -> Dict[str, set]:
    """库里已确认的 现象(归一名)→ {根因名}:新案例同现象、不同根因时要它写区分证据。"""
    out: Dict[str, set] = {}
    try:
        triples, _ = gf.load_triples(kb)
    except Exception:                                     # noqa: BLE001  只用于提示
        return out
    for tr in triples:
        if tr.rel == "caused_by" and tr.in_path:
            out.setdefault(kbtext.normalize(tr.src.name), set()).add(tr.dst.name)
    return out


def _lookalike_note(cand: Dict[str, Any], sections: Dict[str, Any], causes: Dict[str, set]) -> str:
    if str(sections.get("区分证据") or "").strip() and not str(sections.get("区分证据")).startswith("<"):
        return ""
    for ed in cand.get("edges") or []:
        if not isinstance(ed, dict) or ed.get("rel") != "caused_by":
            continue
        sym = kbtext.normalize(str((ed.get("src") or {}).get("name") or ""))
        root = str((ed.get("dst") or {}).get("name") or "")
        others = sorted(r for r in causes.get(sym, set()) if kbtext.normalize(r) != kbtext.normalize(root))
        if others:
            return (f"   ⚠ 同现象「{(ed.get('src') or {}).get('name')}」本行已有根因「{others[0]}」"
                    "——看起来相同、根因不同,请补「区分证据」小节")
    return ""


def _source_of(item_path: pathlib.Path) -> str:
    try:
        meta, _, _ = kbcases.split_frontmatter(item_path.read_text(encoding="utf-8"))
        return str((meta or {}).get("source") or item_path.name)
    except OSError:
        return item_path.name


def render_review(slug: str, entries: List[Dict[str, Any]], errors: List[str]) -> str:
    cases = sorted({e["case_id"] for e in entries})
    n_edges = sum(1 for e in entries if e["kind"] == "edge")
    lines = [f"# 导入确认:{slug}({len(cases)} 案例 / {len(entries)} 项待定 / 其中 {n_edges} 条边无默认)",
             "回复格式:`接受 1-9,12` / `拒绝 10` / `改 3: severity=S2` / `全部接受(边除外)`;"
             "边不答 = 保留为候选(不进「本行历史路径」)", ""]
    for e in errors:
        lines.append(f"- [error] {e}")
    if errors:
        lines.append("")
    for cid in cases:
        lines.append(f"## {cid}")
        for e in entries:
            if e["case_id"] != cid:
                continue
            q = f"   摘录:\"{str(e['quote'])[:60]}\"" if e.get("quote") else ""
            d = {"accept": "建议:接受", "reject": "建议:拒绝", "ask": ""}[e["default"]]
            lines.append(f"{e['n']:>3}. {e['text']}{q}{('   ' + d) if d else ''}")
        lines.append("")
    return "\n".join(lines)


def cmd_review(args: argparse.Namespace, kb: pathlib.Path) -> int:
    inbox = kb / "inbox" / args.slug
    cand_path = inbox / "candidates.json"
    if not cand_path.is_file():
        raise CaseCmdError(f"没有 {cand_path.relative_to(kb)}(先 propose,模型填候选)")
    candidates = _read_json(cand_path)
    if not isinstance(candidates, list):
        raise CaseCmdError("candidates.json 顶层必须是数组")
    entries, errors = review_candidates(kb, args.slug, candidates)
    (inbox / "review.md").write_text(render_review(args.slug, entries, errors), encoding="utf-8")
    _write_json(inbox / "review.json", {"slug": args.slug, "entries": entries, "errors": errors})
    print((inbox / "review.md").read_text(encoding="utf-8"))
    print(f"选择列表     : {(inbox / 'review.md').relative_to(kb)}(向用户原样呈现;回答写成 decisions 后 apply)")
    print(f"Next: python3 kb.py apply {args.slug} --accept <编号> [--reject …] [--edit …] [--all-but-edges]")
    return 2 if errors or entries else 0


# ---------------------------------------------------------------- apply

def parse_numbers(spec: str) -> List[int]:
    out: List[int] = []
    for part in (spec or "").replace(",", " ").split():
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def _decisions(args: argparse.Namespace, entries: List[Dict[str, Any]]) -> Dict[int, Tuple[str, Optional[str]]]:
    """编号 → (accept|reject|edit, 改写值)。未提及:字段/实体按 default,边为 ask(保留候选)。"""
    dec: Dict[int, Tuple[str, Optional[str]]] = {}
    if args.decisions:
        data = yaml.safe_load(pathlib.Path(args.decisions).read_text(encoding="utf-8")) or {}
        for n in parse_numbers(str(data.get("accept") or "")):
            dec[n] = ("accept", None)
        for n in parse_numbers(str(data.get("reject") or "")):
            dec[n] = ("reject", None)
        for n, v in (data.get("edit") or {}).items():
            dec[int(n)] = ("edit", str(v))
        if data.get("all_but_edges"):
            args.all_but_edges = True
    for n in parse_numbers(args.accept or ""):
        dec[n] = ("accept", None)
    for n in parse_numbers(args.reject or ""):
        dec[n] = ("reject", None)
    for item in args.edit or []:
        if ":" not in item:
            raise CaseCmdError(f"--edit 形如 3:severity=S2,拿到 {item!r}")
        n, v = item.split(":", 1)
        dec[int(n)] = ("edit", v.strip())
    for e in entries:
        if e["n"] in dec:
            continue
        if e["kind"] == "edge":
            dec[e["n"]] = ("ask", None)
        elif args.all_but_edges or e["default"] in ("accept", "reject"):
            dec[e["n"]] = (e["default"] if e["default"] != "ask" else "reject", None)
        else:
            dec[e["n"]] = ("reject", None)
    return dec


def _case_markdown(cid: str, case: Dict[str, Any], source: str, entered_by: str) -> str:
    import datetime
    front = {
        "id": cid, "title": case.get("title", ""), "system": case.get("system", ""),
        "instance": case.get("instance") or "未知", "occurred_at": case.get("occurred_at", ""),
        "engine": case.get("engine", ""), "severity": case.get("severity", ""),
        "primary_factor": case.get("primary_factor", ""),
        "secondary_factors": list(case.get("secondary_factors") or []),
        "conclusion": case.get("conclusion", ""), "source": source,
        "entered_by": entered_by, "entered_at": datetime.date.today().isoformat(),
        "objects": list(case.get("objects") or []), "signals": list(case.get("signals") or []),
        "rules": list(case.get("rules") or []),
    }
    topics = [str(x) for x in (case.get("topics") or []) if str(x).strip() and not str(x).startswith("<")]
    if topics:
        front["topics"] = topics
    if case.get("evidence"):
        front["evidence"] = list(case.get("evidence") or [])
    body = "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip() + "\n---\n"
    for s in kbcases.SECTIONS:
        val = str((case.get("sections") or {}).get(s) or "").strip()
        if val.startswith("<") and val.endswith(">"):       # 模板占位没改就当没写
            val = ""
        if val or s in kbcases.REQUIRED_SECTIONS:
            body += f"## {s}\n{val}\n"
    return body


def cmd_apply(args: argparse.Namespace, kb: pathlib.Path) -> int:
    """落盘期间持知识库写锁:cases/、graph/*.yaml、canonical.yaml 是多个导入者共享的文件。"""
    with kblock.hold(kb):
        return _apply_locked(args, kb)


def _apply_locked(args: argparse.Namespace, kb: pathlib.Path) -> int:
    inbox = kb / "inbox" / args.slug
    review_path = inbox / "review.json"
    if not review_path.is_file():
        raise CaseCmdError(f"没有 {review_path.relative_to(kb)}(先 review)")
    review = _read_json(review_path)
    entries: List[Dict[str, Any]] = review.get("entries") or []
    if review.get("errors"):
        raise CaseCmdError("review 还有 [error],先修候选再 apply:\n  " + "\n  ".join(review["errors"]))
    dec = _decisions(args, entries)
    by_case: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        by_case.setdefault(e["case_id"], {"case": None, "fields": {}, "entities": [], "merges": [], "edges": []})
        slot = by_case[e["case_id"]]
        d, val = dec[e["n"]]
        if e["kind"] == "case":
            slot["case"] = (d, e["payload"])
        elif e["kind"] == "field":
            slot["fields"][e["field"]] = (d, val if d == "edit" else e["payload"]["value"])
        elif e["kind"] == "entity":
            slot["entities"].append((d, e["payload"]))
        elif e["kind"] == "merge":
            slot["merges"].append((d, e["payload"]))
        elif e["kind"] == "edge":
            slot["edges"].append((d, e["payload"]))

    aliases_path = kb / "graph" / "canonical.yaml"
    canonical = yaml.safe_load(aliases_path.read_text(encoding="utf-8")) if aliases_path.is_file() else {}
    canonical = canonical or {}
    alias_map, _ = gf.load_canonical(kb)
    written_cases, triples_out = [], []
    entered_by = args.user or "kb"
    defaults = load_strategy(kb).get("defaults", "")
    for cid, slot in by_case.items():
        if slot["case"] is None or slot["case"][0] == "reject":
            continue
        case = dict(slot["case"][1]["case"])
        if not case.get("engine"):                     # 缺省元数据来自策略确认(没确认就按 gaussdb/S3)
            case["engine"] = "opengauss" if "opengauss" in defaults.lower() else "gaussdb"
        if not case.get("severity"):
            m = re.search(r"severity=(S[1-4])", defaults)
            case["severity"] = m.group(1) if m else "S3"
        for fld, (d, val) in slot["fields"].items():
            if d == "reject":
                case[fld] = ""
            elif d == "edit":
                case[fld] = val
        case["objects"] = [p["name"] for d, p in slot["entities"] if d != "reject" and p["kind"] in ("object", "guc", "wait_event", "error")] or list(case.get("objects") or [])
        for d, p in slot["merges"]:
            if d == "accept":
                canonical.setdefault(p["into"], [])
                if p["name"] not in canonical[p["into"]]:
                    canonical[p["into"]].append(p["name"])
                alias_map[kbtext.normalize(p["name"])] = p["into"]
        path = kb / "cases" / f"{cid}.md"
        write_text_atomic(path, _case_markdown(cid, case, slot["case"][1]["source"], entered_by))
        written_cases.append(cid)
        item_file = slot["case"][1]["item_file"]
        conf = kbcases.CONCLUSION_CONFIDENCE.get(str(case.get("conclusion")), 0.3)
        triples_out.append({"src": {"kind": "case", "name": case.get("title", cid), "canonical": f"case:{cid}"},
                            "rel": "exhibits",
                            "dst": {"kind": "symptom", "name": str(case.get("signals", [case.get('title')])[0] if case.get("signals") else case.get("title"))},
                            "confidence": conf, "status": "accepted", "source": f"cases/{cid}.md#现场", "case": cid,
                            "valid_from": case.get("occurred_at") or None})
        for d, p in slot["edges"]:
            if d == "reject":
                status, conf_e = "rejected", float(p["confidence"])
            elif d == "accept":
                status, conf_e = "accepted", 1.0
            else:
                status, conf_e = "candidate", min(float(p["confidence"]), 0.9)
            src = {"kind": p["src"].get("kind", "symptom"), "name": p["src"]["name"]}
            dst = {"kind": p["dst"].get("kind", "rootcause"), "name": p["dst"]["name"]}
            for ref in (src, dst):
                key = kbtext.normalize(ref["name"])
                if key in alias_map:
                    ref["canonical"] = alias_map[key]
            triples_out.append({"src": src, "rel": p["rel"], "dst": dst, "confidence": conf_e, "status": status,
                                "source": f"cases/{cid}.md#{'处置' if p['rel'] == 'handled_by' else '判断'}",
                                "case": cid, "valid_from": case.get("occurred_at") or None})
        try:
            (kb / item_file).unlink()
        except OSError:
            pass

    if canonical:
        write_text_atomic(aliases_path, yaml.safe_dump(canonical, allow_unicode=True, sort_keys=True))
    if triples_out:
        gpath = kb / "graph" / f"{args.slug}.yaml"
        existing = yaml.safe_load(gpath.read_text(encoding="utf-8")) if gpath.is_file() else []
        write_text_atomic(gpath, yaml.safe_dump((existing or []) + triples_out, allow_unicode=True, sort_keys=False))
    _write_json(inbox / "decisions.applied.json", {str(k): v for k, v in dec.items()})
    for p in (inbox / "review.json", inbox / "review.md", inbox / "candidates.json"):
        if p.exists():
            p.unlink()
    if (inbox / "decisions.yaml").exists():
        (inbox / "decisions.yaml").unlink()
    n_edges_ok = sum(1 for t in triples_out if t["status"] == "accepted" and t["rel"] != "exhibits")
    n_edges_cand = sum(1 for t in triples_out if t["status"] == "candidate")
    print(f"写入案例     : {len(written_cases)} 份 → cases/" + ("(" + "、".join(written_cases[:5]) + ")" if written_cases else ""))
    print(f"写入三元组   : {len(triples_out)} 条 → graph/{args.slug}.yaml(已确认边 {n_edges_ok},候选边 {n_edges_cand} 不进路径)")
    remaining = list((inbox / "items").glob("*.md")) if (inbox / "items").is_dir() else []
    print(f"剩余工单     : {len(remaining)} 单" + ("(继续 propose)" if remaining else "(本批完成)"))
    print("Next: python3 kb.py validate && python3 kb.py index")
    return 0


# ---------------------------------------------------------------- parser wiring

def add_subcommands(sub: "argparse._SubParsersAction") -> None:
    p = sub.add_parser("propose", help="出工作单(原文 + 案例 schema + 已知实体),模型据此写 candidates.json")
    p.add_argument("slug")
    p.add_argument("--kb")
    p.add_argument("--batch", type=int, default=WORK_BATCH)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--use-defaults", action="store_true",
                   help="用户说「全按默认」:脚本代写 strategies/tickets.yaml(8 题默认答案)后再出工作单")
    p.set_defaults(func=lambda a: cmd_propose(a, _kb(a)))

    p = sub.add_parser("review", help="校验候选并生成编号选择列表(写库前唯一闸门)")
    p.add_argument("slug")
    p.add_argument("--kb")
    p.set_defaults(func=lambda a: cmd_review(a, _kb(a)))

    p = sub.add_parser("apply", help="按编号决定落盘:cases/*.md、graph/<slug>.yaml、canonical.yaml")
    p.add_argument("slug")
    p.add_argument("--kb")
    p.add_argument("--accept", help="如 1-9,12")
    p.add_argument("--reject", help="如 10")
    p.add_argument("--edit", action="append", help="如 3:severity=S2(可多次)")
    p.add_argument("--all-but-edges", action="store_true", help="字段/实体全按建议接受;边仍需逐条")
    p.add_argument("--decisions", help="decisions.yaml:{accept: '1-9', reject: '10', edit: {3: 'S2'}, all_but_edges: true}")
    p.add_argument("--user", help="录入人工号")
    p.set_defaults(func=lambda a: cmd_apply(a, _kb(a)))
