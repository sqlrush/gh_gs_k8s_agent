"""知识类型登记表 + 类型化条目(md + frontmatter)的解析与校验。

八类知识(2026-10-24 定):规范、案例两类沿用原有格式(rules/*.yaml 条款、cases/*.md 案例);
其余六类是本模块管的「类型化条目」——一条一个 md 文件,放在按类型分的目录里,文件名即 ID:

    wiki/       存量 Wiki 经验        WK-0001.md
    product/    高斯产品文档          PD-0001.md
    runbooks/   处置与恢复手册        RB-LCK-001.md
    defects/    已知缺陷与版本例外    KD-0001.md
    baselines/  指标口径与正常基线    BL-CBST-ACTIVE_SESSIONS.md
    profiles/   环境与业务画像        EP-CBST.md

**类型 = 目录**,frontmatter 的 `type` 必须与目录一致(条目被复制到别处也能自我说明)。
每类有自己的「必须记什么」:硬性缺项是 error(进不了库),要点缺项是 warn(提示补)。
所有类型硬性要求的只有:id / type / title / 摘要 / 出处 / 专题——画像、基线不按专题归,画像也不要求出处。
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .cases import split_frontmatter, split_sections

SUMMARY_SECTION = "摘要"
SUMMARY_SOFT_LIMIT = 300          # 字;L1 里只显示摘要,太长就失去「摘要」的意义
VERIFY_STATES = ("已验证", "未验证", "已过时")
VENDOR_STATES = ("已确认", "待确认")
VALUE_KINDS = ("累计值", "差值", "瞬时值")
PRODUCT_FAMILIES = ("GaussDB 集中式", "GaussDB 分布式", "openGauss", "PostgreSQL", "通用")


@dataclass(frozen=True)
class KnowledgeType:
    code: str                         # 类型代码(frontmatter 的 type)
    dir: str                          # 存放目录
    name: str                         # 设计稿里的全称
    short: str                        # 引用时的前缀:〔手册 RB-LCK-001〕
    id_re: str
    id_hint: str
    required_meta: Tuple[str, ...] = ()
    recommended_meta: Tuple[str, ...] = ()
    recommended_sections: Tuple[str, ...] = ()
    needs_topics: bool = True
    needs_source: bool = True
    enums: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

    def id_ok(self, item_id: str) -> bool:
        return bool(re.fullmatch(self.id_re, item_id))


# 规范(norm)与案例(case)不在这张表里:它们的格式由 rules/*.yaml 与 cases.py 管,这里只登记名字,
# 供 L0 / L1 / 引用前缀统一使用。顺序就是 L0 / L1 / 知识库一节里的展示顺序。
NORM, ERRATA, CASE, GUIDE = "norm", "errata", "case", "guide"
TYPED: Dict[str, KnowledgeType] = {t.code: t for t in (
    KnowledgeType(
        "runbook", "runbooks", "处置与恢复手册", "手册", r"RB-[A-Z]{2,4}-\d{3}", "RB-<域>-NNN,如 RB-LCK-001",
        recommended_meta=("verification",),
        recommended_sections=("适用场景", "执行前提", "影响", "步骤", "验证方法", "回退步骤"),
        enums=(("verification", VERIFY_STATES),)),
    KnowledgeType(
        "defect", "defects", "已知缺陷与版本例外", "缺陷", r"KD-\d{4}", "KD-NNNN,如 KD-0001",
        required_meta=("affected_versions",), recommended_meta=("vendor_status", "fixed_in"),
        recommended_sections=("现象", "复现条件", "规避办法"),
        enums=(("vendor_status", VENDOR_STATES),)),
    KnowledgeType(
        "baseline", "baselines", "指标口径与正常基线", "基线", r"BL-[A-Z0-9_]+-[A-Z0-9_]+", "BL-<系统>-<指标>,如 BL-CBST-ACTIVE_SESSIONS",
        required_meta=("system", "metric"), recommended_meta=("unit", "metric_source", "window", "value_kind"),
        recommended_sections=("正常表现",), needs_source=False,
        enums=(("value_kind", VALUE_KINDS),)),
    KnowledgeType(
        "product", "product", "高斯产品文档", "产品文档", r"PD-\d{4}", "PD-NNNN,如 PD-0001",
        required_meta=("product", "versions"), recommended_meta=("deployment",),
        enums=(("product", PRODUCT_FAMILIES),)),
    KnowledgeType(
        "wiki", "wiki", "存量 Wiki 经验", "Wiki", r"WK-\d{4}", "WK-NNNN,如 WK-0001",
        recommended_meta=("verification", "as_of"), enums=(("verification", VERIFY_STATES),)),
    KnowledgeType(
        "profile", "profiles", "环境与业务画像", "画像", r"EP-[A-Z0-9_]+", "EP-<系统>,如 EP-CBST",
        required_meta=("system",), recommended_meta=("product", "version", "deployment", "aliases"),
        recommended_sections=("部署拓扑", "连接池", "批量窗口", "关键业务依赖", "高峰时段"),
        needs_topics=False, needs_source=False, enums=(("product", PRODUCT_FAMILIES),)),
)}

# 全部八类(+ 修正、旧指南两种存放形态)的名字;展示顺序:修正 > 规范 > 手册 > 缺陷 > 基线 > 案例 > 产品文档 > Wiki > 指南
TYPE_NAMES: Dict[str, Tuple[str, str]] = {
    ERRATA: ("公司内部规范(修正)", "修正"),
    NORM: ("公司内部规范", "规范"),
    "runbook": (TYPED["runbook"].name, TYPED["runbook"].short),
    "defect": (TYPED["defect"].name, TYPED["defect"].short),
    "baseline": (TYPED["baseline"].name, TYPED["baseline"].short),
    CASE: ("历史案例排查思路", "案例"),
    "product": (TYPED["product"].name, TYPED["product"].short),
    "wiki": (TYPED["wiki"].name, TYPED["wiki"].short),
    "profile": (TYPED["profile"].name, TYPED["profile"].short),
    GUIDE: ("指南(类型待确认)", "指南"),
}
DISPLAY_ORDER = tuple(TYPE_NAMES)
# 八类 → 存放目录(L0 的知识范围按这张表数)
EIGHT_TYPES = (NORM, CASE, "profile", "baseline", "defect", "runbook", "product", "wiki")

# 画像里不许出现的东西:IP、主机:端口、口令。画像只记连接别名——它会进 L0,每次诊断都读。
_SENSITIVE = (
    (re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"), "IP 地址"),
    (re.compile(r"\b[A-Za-z][\w.-]*\.[A-Za-z]{2,}:\d{2,5}\b"), "主机名:端口"),
    (re.compile(r"(?i)(password|passwd|口令|密码)\s*[:=:]"), "口令"),
)


@dataclass(frozen=True)
class Item:
    id: str
    type: str
    title: str
    summary: str
    path: pathlib.Path
    topics: Tuple[str, ...] = ()
    applies: Dict[str, Any] = field(default_factory=dict)
    not_applies: str = ""
    source: str = ""
    as_of: str = ""
    verification: str = ""
    evidence: Tuple[Any, ...] = ()
    synonyms: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)
    sections: Dict[str, str] = field(default_factory=dict)
    content_hash: str = ""


def as_list(value: Any) -> Tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return tuple(s.strip() for s in re.split(r"[,,、]", str(value)) if s.strip())


_BY_DIR = {t.dir: t.code for t in TYPED.values()}
TYPED_DIRS = tuple(t.dir for t in TYPED.values())


def type_of_dir(path: pathlib.Path, kb: pathlib.Path) -> Optional[str]:
    try:
        top = path.relative_to(kb).parts[0]
    except (ValueError, IndexError):
        return None
    return _BY_DIR.get(top)


def sensitive_hits(text: str) -> List[str]:
    return [what for rx, what in _SENSITIVE if rx.search(text)]


def _check_enums(kt: KnowledgeType, meta: Dict[str, Any], errors: List[str]) -> None:
    for key, allowed in kt.enums:
        val = str(meta.get(key) or "").strip()
        if val and val not in allowed:
            errors.append(f"{key} `{val}` 非法,只能是 {'/'.join(allowed)}")


def parse_item(text: str, path: pathlib.Path, type_code: str
               ) -> Tuple[Optional[Item], List[str], List[str]]:
    """(Item 或 None, errors, warnings)。有 error 时也尽量返回 Item(validate 要报全)。"""
    kt = TYPED[type_code]
    errors: List[str] = []
    warns: List[str] = []
    meta, body, err = split_frontmatter(text)
    if err:
        return None, [err], []
    if not meta:
        return None, [f"缺少 frontmatter(至少要 id/type/title{'/source' if kt.needs_source else ''})"], []
    item_id = str(meta.get("id") or "").strip()
    if not item_id:
        errors.append("缺少 id")
    elif not kt.id_ok(item_id):
        errors.append(f"id `{item_id}` 不符合{kt.name}的格式 {kt.id_hint}")
    elif path.stem != item_id:
        errors.append(f"id `{item_id}` 与文件名 `{path.stem}` 不一致(文件名即 ID)")
    declared = str(meta.get("type") or "").strip()
    if declared != type_code:
        errors.append(f"type `{declared or '(空)'}` 与所在目录 {kt.dir}/ 不一致——类型由目录决定,两处必须一致")
    title = str(meta.get("title") or "").strip()
    if not title:
        errors.append("缺少 title")
    sections = split_sections(body)
    summary = sections.get(SUMMARY_SECTION, "").strip()
    if not summary:
        errors.append(f"缺少 `## {SUMMARY_SECTION}`(L1 专题导航里只显示这一段)")
    elif len(summary) > SUMMARY_SOFT_LIMIT:
        warns.append(f"摘要 {len(summary)} 字,超过 {SUMMARY_SOFT_LIMIT}——摘要只写结论,细节放后面的小节")
    topics = as_list(meta.get("topics"))
    if kt.needs_topics and not topics:
        errors.append("缺少 topics(所属专题,见 topics/_taxonomy.yaml 或 kb.py topic --list)")
    source = str(meta.get("source") or "").strip()
    if kt.needs_source and not source:
        errors.append("缺少 source(原文出处)")
    for key in kt.required_meta:
        if not str(meta.get(key) or "").strip():
            errors.append(f"缺少 {key}({kt.name}必填)")
    for key in kt.recommended_meta:
        if meta.get(key) in (None, "", []):
            warns.append(f"建议补 {key}")
    for sec in kt.recommended_sections:
        if not sections.get(sec):
            warns.append(f"建议补小节 `## {sec}`")
    _check_enums(kt, meta, errors)
    applies = meta.get("applies") or {}
    if not isinstance(applies, dict):
        errors.append("applies 必须是键值映射(product / versions / systems / deployment)")
        applies = {}
    if type_code == "profile":
        hits = sensitive_hits(text)
        if hits:
            errors.append(f"画像里出现了{'、'.join(hits)}——画像会进 L0、每次诊断都读,只能写连接别名,不能写地址与口令")
    verification = str(meta.get("verification") or meta.get("vendor_status") or "").strip()
    item = Item(
        id=item_id, type=type_code, title=title, summary=summary, path=path, topics=topics,
        applies=applies, not_applies=str(meta.get("not_applies") or "").strip(), source=source,
        as_of=str(meta.get("as_of") or "").strip(), verification=verification,
        evidence=tuple(meta.get("evidence") or ()) if isinstance(meta.get("evidence"), list)
        else as_list(meta.get("evidence")),
        synonyms=as_list(meta.get("synonyms")), aliases=as_list(meta.get("aliases")),
        meta=dict(meta), sections=sections,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])
    return item, errors, warns


def iter_item_files(kb: pathlib.Path, types: Optional[Sequence[str]] = None) -> List[Tuple[str, pathlib.Path]]:
    out: List[Tuple[str, pathlib.Path]] = []
    for code in (types or TYPED):
        root = kb / TYPED[code].dir
        if root.is_dir():
            out += [(code, p) for p in sorted(root.rglob("*.md")) if p.is_file()]
    return out


def load_items(kb: pathlib.Path, types: Optional[Sequence[str]] = None
               ) -> Tuple[List[Item], List[Tuple[str, str]]]:
    """全部类型化条目 + (level, message);有 error 的条目不进返回列表(与 cases.load_cases 同一口径)。"""
    items: List[Item] = []
    findings: List[Tuple[str, str]] = []
    seen: Dict[str, str] = {}
    for code, path in iter_item_files(kb, types):
        rel = path.relative_to(kb).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(("error", f"{rel}: 不是 UTF-8 编码"))
            continue
        except OSError as exc:
            findings.append(("error", f"{rel}: 读取失败:{exc}"))
            continue
        item, errors, warns = parse_item(text, path, code)
        findings += [("error", f"{rel}: {e}") for e in errors]
        findings += [("warn", f"{rel}: {w}") for w in warns]
        if item is None or errors:
            continue
        if item.id in seen:
            findings.append(("error", f"{rel}: ID `{item.id}` 与 {seen[item.id]} 重复"))
            continue
        seen[item.id] = rel
        items.append(item)
    return items, findings


def next_id(kb: pathlib.Path, type_code: str, hint: str = "") -> str:
    """给新条目分配 ID。序号类(WK/PD/KD)取当前最大号 +1;RB 要域(hint,如 LCK);EP/BL 由系统/指标拼。
    改过类型的旧 ID 记在别的条目的 aliases 里,也算占用——ID 永不复用。"""
    kt = TYPED[type_code]
    taken = {p.stem for _c, p in iter_item_files(kb)}
    for _c, p in iter_item_files(kb):
        try:
            meta, _b, _e = split_frontmatter(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        taken.update(as_list((meta or {}).get("aliases")))
    if type_code in ("profile", "baseline"):
        parts = [re.sub(r"[^A-Z0-9_]", "_", s.upper()).strip("_") for s in hint.split("/") if s.strip()]
        prefix = "EP" if type_code == "profile" else "BL"
        cand = "-".join([prefix] + parts)
        if not kt.id_ok(cand):
            raise ValueError(f"{kt.name}的 ID 由{'系统' if prefix == 'EP' else '系统/指标'}拼成,拿到 {hint!r}")
        return cand
    if type_code == "runbook":
        dom = re.sub(r"[^A-Z]", "", (hint or "GEN").upper())[:4] or "GEN"
        dom = dom if len(dom) >= 2 else (dom + "X")
        prefix, width = f"RB-{dom}-", 3
    else:
        prefix, width = {"wiki": "WK-", "product": "PD-", "defect": "KD-"}[type_code], 4
    nums = [int(t[len(prefix):]) for t in taken if t.startswith(prefix) and t[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1 if nums else 1):0{width}d}"
