"""案例文件 <kb>/cases/S1-日期-系统-标题.md —— 解析与校验(纯函数 + 文件 I/O,无库)。

格式参考 gaussdb-rootcause 的 S1 文件:YAML frontmatter + 现场 / 判断 / 处置 三个必填小节。
frontmatter 是检索与图的结构化入口,正文给模型读。

2026-10-24 起案例记「排查思路」,不只记结论:可选小节 候选原因 / 取证顺序 / 排除理由 / 最终验证,
以及「区分证据」——同一现象在本行有过不同根因时,靠它区分(看起来相同、根因不同的反例)。
frontmatter 可选 topics(所属专题)与 evidence(指向 L3 原始证据)。

「结论强度」是整个案例所有边的置信度来源:已确认 1.0 / 推测 0.6 / 待验证 0.3。
只有 1.0 的边进「本行历史路径」——所以这个字段校验得最严。
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

CASE_ID_RE = re.compile(r"^S[1-4]-\d{8}-[A-Za-z0-9_]+-\S+$")
REQUIRED_FIELDS = ("id", "title", "system", "occurred_at", "conclusion", "source")
# 顺序即写盘顺序:先现场,再排查过程,再结论与处置,最后是再发时怎么认、怎么和相似的区分
SECTIONS = ("现场", "候选原因", "取证顺序", "排除理由", "判断", "处置", "最终验证", "复发标志", "区分证据")
TROUBLESHOOTING_SECTIONS = ("候选原因", "取证顺序", "排除理由", "最终验证")
REQUIRED_SECTIONS = ("现场", "判断", "处置")
CONCLUSION_CONFIDENCE = {"已确认": 1.0, "推测": 0.6, "待验证": 0.3}
SEVERITIES = ("S1", "S2", "S3", "S4")
_HEADING_RE = re.compile(r"^##\s+(\S+?)\s*$", re.M)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    system: str
    occurred_at: str
    conclusion: str
    source: str
    path: pathlib.Path
    instance: str = ""
    engine: str = ""
    severity: str = ""
    primary_factor: str = ""
    secondary_factors: Tuple[str, ...] = ()
    entered_by: str = ""
    entered_at: str = ""
    objects: Tuple[str, ...] = ()
    signals: Tuple[str, ...] = ()
    rules: Tuple[str, ...] = ()
    topics: Tuple[str, ...] = ()
    evidence: Tuple[str, ...] = ()
    sections: Dict[str, str] = field(default_factory=dict)
    content_hash: str = ""

    @property
    def confidence(self) -> float:
        return CONCLUSION_CONFIDENCE.get(self.conclusion, 0.0)

    def section(self, name: str) -> str:
        return self.sections.get(name, "")


def _as_list(value) -> Tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return (str(value).strip(),)


def split_frontmatter(text: str) -> Tuple[Optional[dict], str, Optional[str]]:
    """(meta, body, error)。没有 frontmatter → ({}, text, None)。"""
    if not text.startswith("---"):
        return {}, text, None
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return None, "", "frontmatter 起始 --- 没有对应的结束 ---"
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        return None, "", f"frontmatter YAML 解析失败:{exc}"
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return None, "", "frontmatter 不是键值映射"
    return meta, text[m.end():], None


def split_sections(body: str) -> Dict[str, str]:
    """`## 现场` … 按二级标题切;标题外的前言丢弃;同名节后者覆盖前者。"""
    out: Dict[str, str] = {}
    marks = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out[m.group(1)] = body[m.end():end].strip()
    return out


def parse_case(text: str, path: pathlib.Path) -> Tuple[Optional[Case], List[str]]:
    """解析一份案例;返回 (Case 或 None, 错误列表)。能解析出来但有缺项时仍返回 Case + 错误。"""
    errors: List[str] = []
    meta, body, err = split_frontmatter(text)
    if err:
        return None, [err]
    if not meta:
        return None, ["缺少 frontmatter(至少要 id/title/system/occurred_at/conclusion/source)"]
    for f in REQUIRED_FIELDS:
        if not str(meta.get(f) or "").strip():
            errors.append(f"缺少必填字段 {f}")
    cid = str(meta.get("id") or "").strip()
    if cid and not CASE_ID_RE.match(cid):
        errors.append(f"id `{cid}` 不符合 S<1-4>-YYYYMMDD-<系统>-<标题> 格式")
    if cid and path.stem != cid:
        errors.append(f"id `{cid}` 与文件名 `{path.stem}` 不一致(文件名即案例 ID)")
    conclusion = str(meta.get("conclusion") or "").strip()
    if conclusion and conclusion not in CONCLUSION_CONFIDENCE:
        errors.append(f"conclusion `{conclusion}` 非法,只能是 {'/'.join(CONCLUSION_CONFIDENCE)}"
                      "(它决定本案例所有边的置信度)")
    occurred = str(meta.get("occurred_at") or "").strip()
    if occurred and not _DATE_RE.match(occurred):
        errors.append(f"occurred_at `{occurred}` 不是 YYYY-MM-DD")
    severity = str(meta.get("severity") or "").strip()
    if severity and severity not in SEVERITIES:
        errors.append(f"severity `{severity}` 只能是 {'/'.join(SEVERITIES)}")
    sections = split_sections(body)
    for s in REQUIRED_SECTIONS:
        if not sections.get(s):
            errors.append(f"缺少小节 `## {s}`(或为空)")
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown:
        errors.append(f"未知小节 {unknown},只认 {'/'.join(SECTIONS)}")
    case = Case(
        id=cid, title=str(meta.get("title") or "").strip(), system=str(meta.get("system") or "").strip(),
        occurred_at=occurred, conclusion=conclusion, source=str(meta.get("source") or "").strip(),
        path=path, instance=str(meta.get("instance") or "").strip(),
        engine=str(meta.get("engine") or "").strip(), severity=severity,
        primary_factor=str(meta.get("primary_factor") or "").strip(),
        secondary_factors=_as_list(meta.get("secondary_factors")),
        entered_by=str(meta.get("entered_by") or "").strip(),
        entered_at=str(meta.get("entered_at") or "").strip(),
        objects=_as_list(meta.get("objects")), signals=_as_list(meta.get("signals")),
        rules=_as_list(meta.get("rules")), topics=_as_list(meta.get("topics")),
        evidence=_as_list(meta.get("evidence")), sections=sections,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    )
    return case, errors


def iter_case_files(kb: pathlib.Path) -> List[pathlib.Path]:
    root = kb / "cases"
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def load_cases(kb: pathlib.Path) -> Tuple[List[Case], List[Tuple[str, str]]]:
    """全部案例 + (level, message) 发现;有 error 的案例不进返回列表。"""
    cases: List[Case] = []
    findings: List[Tuple[str, str]] = []
    seen: Dict[str, str] = {}
    for path in iter_case_files(kb):
        rel = str(path.relative_to(kb))
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(("error", f"{rel}: 不是 UTF-8 编码"))
            continue
        except OSError as exc:
            findings.append(("error", f"{rel}: 读取失败:{exc}"))
            continue
        case, errors = parse_case(text, path)
        for e in errors:
            findings.append(("error", f"{rel}: {e}"))
        if case is None or errors:
            continue
        if case.id in seen:
            findings.append(("error", f"{rel}: 案例 ID `{case.id}` 与 {seen[case.id]} 重复"))
            continue
        seen[case.id] = rel
        if not case.sections.get("复发标志") and not case.signals:
            findings.append(("warn", f"{rel}: 没有「复发标志」也没有 signals——按发现匹配案例靠它,建议补"))
        if case.source and "#" not in case.source:
            findings.append(("warn", f"{rel}: source 没有 #小节 定位(形如 sources/x.docx#前言)"))
        if case.conclusion != "已确认":
            findings.append(("warn", f"{rel}: 结论强度「{case.conclusion}」——本案例的边不入「本行历史路径」,只作候选参照"))
        cases.append(case)
    return cases, findings
