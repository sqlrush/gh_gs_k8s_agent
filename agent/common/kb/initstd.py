"""入库前的标准化:五花八门的工单材料 → 统一格式的六要素 md。

知识库导入(gaussdb-kb-import)吃的是结构化材料,而客户手里的是 word / pdf / 截图 / 随手记的 txt。
这一层把它们先拉平成同一种 md:frontmatter 记**时间、系统**与出处,正文固定四段
**现场 / 判断 / 处置 / 复发标志**。段名与 `cases/` 的案例格式一致,后面 ingest → propose →
选择列表 → apply 的流程一个字都不用改。

分工与整个知识库一脉相承:
  **脚本**做确定性的——分类文件、抽文本、日期归一、渲染、校验;
  **模型**做语义的——读原文(照片得靠视觉)把六要素写出来。

两条硬规矩,都是防静默失效的:
  1. 六要素缺一不可。原文真没写,要**显式**写「原文未写明」——空着、TODO、`?` 一律拒绝,
     因为空白到了下游会被当成「没这回事」,而不是「没查到」。
  2. 照片抽出来的内容在文件里标 `extracted_by: model`。它没有可 diff 的原文,复核成本和
     脚本抽的不是一回事,得看得出来。
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

STD_VERSION = 1

# 正文四段:段名与顺序固定。cases/ 的案例格式用的就是这四个名字。
SECTIONS: Tuple[str, ...] = ("现场", "判断", "处置", "复发标志")
# frontmatter 里的两个必填要素(用户口径的「时间、系统」)
REQUIRED_META: Tuple[str, ...] = ("occurred_at", "system")
# 与导入侧 common/kb/cases.CONCLUSION_CONFIDENCE 一致。两边取值口径必须相同:
# 这边只让写两种、导入侧认三种的话,模型在候选里改成第三种就会被当成「与标准化文档不一致」拦下。
CONCLUSIONS: Tuple[str, ...] = ("已确认", "推测", "待验证")
DEFAULT_CONCLUSION = "推测"          # 拿不准时站在保守一侧

# 「原文没写」的合法写法。模型只能在这几个里选,不能自由发挥成「无」「暂无」——
# 那些词读起来像结论,实际是空白。
UNKNOWN_MARKS: Tuple[str, ...] = ("原文未写明", "原文未提及", "未提及")
# 一眼就是没填的占位:拒绝。
_PLACEHOLDER = re.compile(r"^(?:todo|tbd|n/?a|待填|待补|待定|无|暂无|没有|\?+|-+|/+)$", re.I)
_MIN_LEN = 4                         # 四段正文的下限:比这还短的不是内容,是敷衍

TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".sql", ".log"})
DOC_SUFFIXES = frozenset({".docx", ".doc", ".pdf", ".rtf"})
TABLE_SUFFIXES = frozenset({".csv", ".xlsx", ".xls"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
                            ".heic", ".heif"})
# opencode 的 read 工具读不了的图片格式:苹果手机直出的 HEIC 是现场最常见的一种。
# scan 会用 imgconv 自动转成 jpg 副本(导入镜像里装了 pillow / pillow-heif);转不了才请用户自己转
_AWKWARD_IMAGE = frozenset({".heic", ".heif", ".tif", ".tiff"})


class StdFormatError(Exception):
    """格式不合标准。信息里必须点名是哪一项,否则模型只会原样重试。"""


# ---------------------------------------------------------------- 文件分类

@dataclass(frozen=True)
class Classified:
    path: pathlib.Path
    kind: str              # text | doc | table | image | unsupported
    reason: str

    @property
    def needs_vision(self) -> bool:
        return self.kind == "image"

    @property
    def usable(self) -> bool:
        return self.kind != "unsupported"


def classify(path: pathlib.Path) -> Classified:
    """按后缀决定这份材料由脚本抽还是由模型看。"""
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return Classified(path, "text", "纯文本,脚本直接读")
    if suffix in DOC_SUFFIXES:
        return Classified(path, "doc", "文档,脚本经转换器抽文本(扫描件 PDF 无文本层时会拒绝并说明)")
    if suffix in TABLE_SUFFIXES:
        return Classified(path, "table", "表格,脚本按行拆成一行一单")
    if suffix in IMAGE_SUFFIXES:
        reason = "照片/截图,脚本 OCR 不了——**由你用 read 工具看图**,把六要素写进草稿"
        if suffix in _AWKWARD_IMAGE:
            reason += ("；注意这是 %s,read 打不开原件——scan 会自动转成 jpg 副本(看工作单的 image_path),"
                       "转不了时如实告诉用户,请他转成 jpg / png 再传" % suffix.lstrip(".").upper())
        return Classified(path, "image", reason)
    return Classified(path, "unsupported", "不支持的格式 %s" % (suffix or "(无后缀)"))


def classify_all(paths: Iterable[pathlib.Path]) -> List[Classified]:
    return [classify(p) for p in paths]


# ---------------------------------------------------------------- 日期归一

_DATE_PATTERNS = (
    re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)


def normalize_date(raw: str) -> str:
    """工单里的各种写法 → YYYY-MM-DD。认不出来就抛错,**不猜**。

    猜错日期的后果特别隐蔽:案例挂到错的时间线上,检索时看起来一切正常,
    只是「最近有没有类似问题」的答案是错的。
    """
    text = (raw or "").strip()
    if not text:
        raise StdFormatError("occurred_at(时间)为空——原文里的发生日期是多少?拿不准就问用户,不要猜")
    for pat in _DATE_PATTERNS:
        m = pat.match(text)
        if not m:
            continue
        y, mo, d = (int(g) for g in m.groups())
        try:
            return _dt.date(y, mo, d).isoformat()
        except ValueError as exc:
            raise StdFormatError("occurred_at %r 不是合法日期(%s)" % (raw, exc)) from exc
    raise StdFormatError(
        "occurred_at %r 认不出来。支持 2026-01-08 / 2026年1月8日 / 2026/01/08 / 20260108;"
        "原文写的是相对时间(如「上周」)时,问用户要具体日期,不要自己换算" % raw)


# ---------------------------------------------------------------- 渲染

def _clean(value: object) -> str:
    return re.sub(r"[ \t]+$", "", str(value or "").replace("\r\n", "\n").replace("\r", "\n"), flags=re.M).strip()


def _require_section(name: str, value: str) -> str:
    text = _clean(value)
    if not text:
        raise StdFormatError(
            "%s 为空。六要素缺一不可;原文确实没写就写「原文未写明」——"
            "空着到了下游会被读成「没这回事」,而不是「没查到」" % name)
    if _PLACEHOLDER.match(text):
        raise StdFormatError(
            "%s 是占位/搪塞的写法(%r)。原文没写就写「原文未写明」,别用 %s"
            % (name, text[:20], "「无」「TODO」这类词"))
    if text not in UNKNOWN_MARKS and len(text) < _MIN_LEN:
        raise StdFormatError("%s 只有 %d 个字(%r),不像是从原文里抽出来的内容" % (name, len(text), text))
    return text


def _yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _yaml_list(values: Sequence[str]) -> str:
    items = [_clean(v) for v in values or [] if _clean(v)]
    return "[]" if not items else "[" + ", ".join(_yaml_scalar(v) for v in items) + "]"


def normalize_draft(draft: Mapping[str, object], meta: Mapping[str, object]) -> Dict[str, object]:
    """把模型交回的草稿校验并归一成一份可渲染的数据。渲染与合并文件共用这一步。"""
    source = _clean(meta.get("source"))
    if not source:
        raise StdFormatError("缺出处(source)。每一单都必须能指回原文件的具体位置,指不回去的不入库")

    for key in REQUIRED_META:
        if not _clean(draft.get(key)):
            raise StdFormatError("%s 为空——六要素之一(时间 / 系统)" % key)

    conclusion = _clean(draft.get("conclusion")) or DEFAULT_CONCLUSION
    if conclusion not in CONCLUSIONS:
        raise StdFormatError("conclusion 只能是 %s,不是 %r" % (" / ".join(CONCLUSIONS), conclusion))

    item_id = _clean(draft.get("item_id")) or _clean(draft.get("id"))
    title = _clean(draft.get("title")) or (_clean(draft.get("现场"))[:40] if draft.get("现场") else item_id)
    out: Dict[str, object] = {
        "std_version": STD_VERSION,
        "item_id": item_id or "unknown",
        "title": title or "未命名工单",
        "occurred_at": normalize_date(_clean(draft.get("occurred_at"))),
        "system": _clean(draft.get("system")),
        "severity": _clean(draft.get("severity")),
        "conclusion": conclusion,
        "objects": [_clean(v) for v in (draft.get("objects") or []) if _clean(v)],
        "signals": [_clean(v) for v in (draft.get("signals") or []) if _clean(v)],
        "source": source,
        "source_file": _clean(meta.get("source_file")) or source.split("#", 1)[0],
        "source_kind": _clean(meta.get("source_kind")) or "unknown",
        "extracted_by": _clean(meta.get("extracted_by")) or "model",
        "redacted": int(meta.get("redacted") or 0),
    }
    out["sections"] = {name: _require_section(name, draft.get(name)) for name in SECTIONS}
    return out


def render_std(draft: Mapping[str, object], meta: Mapping[str, object]) -> str:
    """统一格式的 md。同样的输入渲染两次必须逐字相同——这个 skill 的价值就在这。"""
    d = normalize_draft(draft, meta)
    lines = ["---",
             "std_version: %d" % d["std_version"],
             "item_id: %s" % _yaml_scalar(d["item_id"]),
             "title: %s" % _yaml_scalar(d["title"]),
             "occurred_at: %s" % _yaml_scalar(d["occurred_at"]),
             "system: %s" % _yaml_scalar(d["system"]),
             "severity: %s" % _yaml_scalar(d["severity"]),
             "conclusion: %s" % _yaml_scalar(d["conclusion"]),
             "objects: %s" % _yaml_list(d["objects"]),
             "signals: %s" % _yaml_list(d["signals"]),
             "source: %s" % _yaml_scalar(d["source"]),
             "source_file: %s" % _yaml_scalar(d["source_file"]),
             "source_kind: %s" % _yaml_scalar(d["source_kind"]),
             "extracted_by: %s" % _yaml_scalar(d["extracted_by"]),
             "redacted: %d" % d["redacted"],
             "---", ""]
    for name in SECTIONS:
        lines += ["## " + name, "", d["sections"][name], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------- 解析与校验

_FM_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")


def parse_std(text: str) -> Tuple[Dict[str, object], Dict[str, str]]:
    """反向解析统一格式:(frontmatter, {段名: 正文})。解析不了就抛 StdFormatError。"""
    body = text.lstrip("﻿")
    if not body.startswith("---\n"):
        raise StdFormatError("文件开头没有 frontmatter(--- 开始的那段)")
    end = body.find("\n---\n", 3)
    if end < 0:
        raise StdFormatError("frontmatter 没有结束的 ---")
    fm: Dict[str, object] = {}
    for line in body[4:end].splitlines():
        m = _FM_LINE.match(line.strip())
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw.startswith("["):
            try:
                fm[key] = json.loads(raw)
            except ValueError:
                fm[key] = []
        elif raw.startswith('"'):
            try:
                fm[key] = json.loads(raw)
            except ValueError:
                fm[key] = raw.strip('"')
        elif raw.isdigit():
            fm[key] = int(raw)
        else:
            fm[key] = raw
    sections: Dict[str, str] = {}
    current = None
    buf: List[str] = []
    for line in body[end + 5:].splitlines():
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return fm, sections


def validate_std(text: str) -> List[str]:
    """返回问题清单;空列表 = 合格。手工改坏了的文件要能被这一条拦住。"""
    try:
        fm, sections = parse_std(text)
    except StdFormatError as exc:
        return [str(exc)]
    problems: List[str] = []
    if fm.get("std_version") != STD_VERSION:
        problems.append("std_version 是 %r,本脚本认的是 %d" % (fm.get("std_version"), STD_VERSION))
    for key in REQUIRED_META + ("source",):
        if not str(fm.get(key) or "").strip():
            problems.append("frontmatter 缺 %s" % key)
    if fm.get("occurred_at"):
        try:
            normalize_date(str(fm["occurred_at"]))
        except StdFormatError as exc:
            problems.append(str(exc))
    if fm.get("conclusion") and fm["conclusion"] not in CONCLUSIONS:
        problems.append("conclusion %r 不在 %s 里" % (fm["conclusion"], " / ".join(CONCLUSIONS)))

    order = [name for name in sections]
    if order != list(SECTIONS):
        missing = [s for s in SECTIONS if s not in sections]
        extra = [s for s in order if s not in SECTIONS]
        if missing:
            problems.append("缺段落:%s" % "、".join(missing))
        if extra:
            problems.append("多出不认识的段落:%s" % "、".join(extra))
        if not missing and not extra:
            problems.append("段落顺序不对:应为 %s" % " → ".join(SECTIONS))
    for name in SECTIONS:
        if name not in sections:
            continue
        try:
            _require_section(name, sections[name])
        except StdFormatError as exc:
            problems.append(str(exc))
    return problems


# ---------------------------------------------------------------- 交给 kb-import 的合并文件

def render_ingest_block(draft: Mapping[str, object], meta: Mapping[str, object]) -> str:
    """一单一块,**不带 frontmatter**。

    kb.py ingest 处理 md 时按 `\\n---\\n` 切多单;块里再带 frontmatter,结束的 --- 会把它从中间劈开,
    变成「半份元数据 + 半份正文」两单。元数据因此写成 `- key: value`,与 xlsx/csv 那条路产出的形状一致。
    """
    d = normalize_draft(draft, meta)
    lines = ["# %s" % d["title"], ""]
    meta_lines = [("id", d["item_id"]), ("system", d["system"]), ("occurred_at", d["occurred_at"]),
                  ("severity", d["severity"]), ("conclusion", d["conclusion"]), ("source", d["source"])]
    lines += ["- %s: %s" % (k, v) for k, v in meta_lines if v]
    if d["objects"]:
        lines.append("- objects: %s" % "、".join(d["objects"]))
    if d["signals"]:
        lines.append("- signals: %s" % "、".join(d["signals"]))
    lines.append("")
    for name in SECTIONS:
        lines += ["## " + name, "", d["sections"][name], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def render_ingest_file(pairs: Sequence[Tuple[Mapping[str, object], Mapping[str, object]]]) -> str:
    """多单合成一个文件,用 ingest 认的 `\\n---\\n` 分隔。"""
    blocks = [render_ingest_block(draft, meta).rstrip("\n") for draft, meta in pairs]
    return "\n---\n".join(blocks) + "\n"


# ---------------------------------------------------------------- 杂

_SAFE = re.compile(r"[^0-9A-Za-z一-鿿]+")


def batch_slug(name: str) -> str:
    slug = _SAFE.sub("-", str(name or "")).strip("-").lower()
    return slug or "batch"
