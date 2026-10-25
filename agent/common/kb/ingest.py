"""工单导入的确定性半边:csv / xlsx / md / txt → 一单一文件(inbox/<slug>/items/*.md)。

xlsx 用 zipfile + xml.etree 直接读(不引 openpyxl);csv 自动识别 utf-8 / gb18030。
列名靠同义词启发式猜(标题/描述/处理过程/解决方案/根因/系统/时间/工单号/级别),
猜的结果只是**建议**,首次导入时会作为策略问题交给用户确认(propose)。

脱敏(--redact)是确定性正则:IPv4、手机号、身份证、邮箱 → 占位符;对象名/表名不动,那是知识本身。
"""
from __future__ import annotations

import csv
import io
import json
import pathlib
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

import yaml

# 列名同义词(小写比较);顺序即优先级
COLUMN_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "id": ("工单号", "工单编号", "单号", "事件编号", "编号", "ticket", "ticket_id", "id", "incident"),
    "title": ("标题", "主题", "问题标题", "摘要", "title", "subject", "summary"),
    "system": ("系统", "业务系统", "应用", "应用系统", "system", "app", "application"),
    "occurred_at": ("发生时间", "故障时间", "发现时间", "报告时间", "创建时间", "时间", "occurred", "created", "date", "time"),
    "severity": ("级别", "严重级别", "严重程度", "优先级", "等级", "severity", "priority", "level"),
    "description": ("问题描述", "现象", "故障现象", "描述", "现场", "问题", "description", "symptom", "issue"),
    "process": ("处理过程", "排查过程", "分析过程", "诊断过程", "过程", "process", "analysis", "diagnosis"),
    "root_cause": ("根因", "根本原因", "原因分析", "原因", "判断", "root_cause", "rootcause", "cause"),
    "solution": ("解决方案", "处置", "处理措施", "处置措施", "解决办法", "措施", "solution", "resolution", "fix", "action"),
    "verification": ("验证", "效果", "恢复情况", "verification", "outcome", "result"),
}
_SECTION_TITLES = {"description": "问题描述", "process": "处理过程", "root_cause": "根因",
                   "solution": "解决方案", "verification": "验证与效果"}

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class IngestError(Exception):
    """格式不支持 / 文件损坏 / 空表。"""


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    text: str                      # 给模型读的 markdown 正文
    locator: str                   # 出处定位,如 tickets.xlsx#row=18
    fields: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------- 表格读取

def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def fit_rows(headers: Sequence[str], rows: Sequence[Sequence[str]], name: str = "") -> List[List[str]]:
    """把每行对齐到表头宽度:短了补空,**长了把多出的格并进最后一列**——绝不静默丢内容。

    没加引号的导出里字段内的逗号会把一行拆成十几格,丢掉就是丢客户的处置过程。
    """
    width = len(headers)
    out: List[List[str]] = []
    overflow = 0
    for r in rows:
        cells = [str(c).strip() for c in r]
        if len(cells) > width:
            overflow += 1
            cells = cells[:width - 1] + [", ".join(c for c in cells[width - 1:] if c)]
        out.append(cells + [""] * (width - len(cells)))
    if overflow:
        import sys
        print(f"警告:{name or '表格'} 有 {overflow} 行的列数多于表头,多出的格已并入最后一列「{headers[-1]}」"
              f"——多半是字段里的逗号没加引号,导入后请核对这些行。", file=sys.stderr)
    return out


def read_csv(path: pathlib.Path) -> Tuple[List[str], List[List[str]]]:
    text = _decode(path.read_bytes())
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise IngestError(f"{path.name}:空表")
    headers = [h.strip() for h in rows[0]]
    return headers, fit_rows(headers, rows[1:], path.name)


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_COL_RE = re.compile(r"^([A-Z]+)")


def _col_index(ref: str) -> int:
    letters = _COL_RE.match(ref).group(1) if _COL_RE.match(ref) else "A"
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _cell_text(c: ET.Element, shared: List[str]) -> str:
    t = c.get("t")
    if t == "s":
        v = c.find("m:v", _NS)
        try:
            return shared[int(v.text)] if v is not None and v.text is not None else ""
        except (ValueError, IndexError):
            return ""
    if t == "inlineStr":
        return "".join(x.text or "" for x in c.iter("{%s}t" % _NS["m"]))
    v = c.find("m:v", _NS)
    return (v.text or "") if v is not None else ""


def read_xlsx(path: pathlib.Path, sheet: int = 1) -> Tuple[List[str], List[List[str]]]:
    """第 sheet 张表;共享字符串与内联字符串都认;日期序列号原样保留(不猜格式)。"""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{path.name}:不是有效的 xlsx(zip 损坏)") from exc
    with zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
        name = f"xl/worksheets/sheet{sheet}.xml"
        if name not in zf.namelist():
            raise IngestError(f"{path.name}:没有第 {sheet} 张工作表")
        root = ET.fromstring(zf.read(name))
        rows: List[List[str]] = []
        for row in root.iter("{%s}row" % _NS["m"]):
            cells: Dict[int, str] = {}
            for c in row.findall("m:c", _NS):
                cells[_col_index(c.get("r") or "A")] = _cell_text(c, shared)
            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(i, "").strip() for i in range(width)])
    rows = [r for r in rows if any(r)]
    if not rows:
        raise IngestError(f"{path.name}:空表")
    headers = rows[0]
    return headers, fit_rows(headers, rows[1:], path.name)


# ---------------------------------------------------------------- 列映射

def guess_columns(headers: Sequence[str]) -> Dict[str, str]:
    """{字段: 列名} 的建议;同一列只映射一次,按 COLUMN_SYNONYMS 的优先级。"""
    used = set()
    out: Dict[str, str] = {}
    lowered = [(h, h.strip().lower()) for h in headers]
    for fld, syns in COLUMN_SYNONYMS.items():
        for syn in syns:
            hit = next((h for h, low in lowered if h not in used and (low == syn or syn in low)), None)
            if hit is not None:
                out[fld] = hit
                used.add(hit)
                break
    return out


def rows_to_items(headers: Sequence[str], rows: Sequence[Sequence[str]], mapping: Dict[str, str],
                  locator_prefix: str) -> List[Item]:
    idx = {h: i for i, h in enumerate(headers)}
    items: List[Item] = []
    for n, row in enumerate(rows, 2):                       # 表头是第 1 行
        get = lambda fld: (row[idx[mapping[fld]]] if fld in mapping and mapping[fld] in idx and idx[mapping[fld]] < len(row) else "").strip()
        tid = get("id") or f"row{n}"
        title = get("title") or (get("description")[:40] if get("description") else tid)
        parts = [f"# {title}", ""]
        meta = [(k, get(k)) for k in ("id", "system", "occurred_at", "severity") if get(k)]
        if meta:
            parts += [f"- {k}: {v}" for k, v in meta] + [""]
        for fld, heading in _SECTION_TITLES.items():
            val = get(fld)
            if val:
                parts += [f"## {heading}", "", val, ""]
        rest = [(h, row[i]) for h, i in idx.items() if i < len(row) and row[i].strip()
                and h not in mapping.values()]
        if rest:
            parts += ["## 原始字段", ""] + [f"- {h}: {v}" for h, v in rest] + [""]
        fields = {h: (row[i] if i < len(row) else "") for h, i in idx.items()}
        items.append(Item(id=str(tid), title=title, text="\n".join(parts).rstrip() + "\n",
                          locator=f"{locator_prefix}#row={n}", fields=fields))
    return items


# ---------------------------------------------------------------- 文本材料

# gaussdb-kb-init 产出的标准化块:已定字段写成 `- key: value`(不能带 frontmatter —— 结束的 ---
# 会被本模块的分隔符从中间劈开)。认 std 的条件是**同时**有 system 与 occurred_at,这两个正是
# kb-init 的必填项;只有 `- id:` 而没有它们的是普通 md,不按 std 认 —— 认错了出处会被那一行顶替。
_META_LINE = re.compile(r"^-\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\S.*?)\s*$")
_STD_REQUIRED = ("system", "occurred_at")


def std_meta(block: str) -> Dict[str, str]:
    """标准化块里的已定字段;不是标准化块就返回 {}。只看第一个 `## ` 之前的元数据行。"""
    meta: Dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        m = _META_LINE.match(stripped)
        if m:
            meta[m.group(1)] = m.group(2)
    return meta if all(meta.get(k) for k in _STD_REQUIRED) else {}


_NO_ID = ("", "unknown", "none", "null", "-", "无")


def _std_item_id(meta: Dict[str, str], title: str, fallback: str) -> str:
    """标准化块的工单号;没号(kb-init 写成 unknown)时用「日期-标题」。

    2026-10-24 测试环境:三份问题分析报告都没有工单号,标准化后都是 unknown,导入时三单写进同一个
    unknown.md,前两单无声无息地没了。"""
    raw = (meta.get("id") or meta.get("item_id") or "").strip()
    if raw.lower() not in _NO_ID:
        return raw
    date = (meta.get("occurred_at") or "").strip()
    return ("%s-%s" % (date, title) if date else title)[:80] if title else fallback


def _std_document_item(text: str, locator_prefix: str, stem: str) -> Optional[Item]:
    """kb-init 的单篇标准化文档(std/*.md,带 YAML frontmatter)直接导入:整篇是一单。
    不认的话会按 `---` 把 frontmatter 切成几单乱码(同一次测试里模型绕路时撞上的)。"""
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return None
    try:
        front = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict) or "std_version" not in front:
        return None
    title = str(front.get("title") or stem).strip()
    meta = {k: str(front.get(k) or "").strip() for k in ("item_id", "system", "occurred_at", "severity", "conclusion")}
    head = "\n".join("- %s: %s" % (k if k != "item_id" else "id", v) for k, v in meta.items() if v)
    fields = {k: v for k, v in meta.items() if k != "item_id" and v}
    fields.update({"ingested_from": locator_prefix, "_std": "1"})
    return Item(id=_std_item_id(meta, title, stem), title=title[:80],
                text="# %s\n\n%s\n\n%s" % (title, head, text[m.end():].strip()) + "\n",
                locator=str(front.get("source") or locator_prefix), fields=fields)


def split_text_items(text: str, locator_prefix: str, stem: str) -> List[Item]:
    """md/txt:用 `\\n---\\n` 分隔多单;没有分隔符就是一单。标题取首个非空行。

    标准化(std)块另按它自己的已定字段走:id 用原始工单号、出处指回**客户原件**,
    我们的中间定位记进 `ingested_from`。否则案例的 source 会指向中间文件,追溯链断在自己手里。
    """
    whole = _std_document_item(text, locator_prefix, stem)
    if whole is not None:
        return [whole]
    blocks = [b.strip() for b in re.split(r"\n-{3,}\n", text) if b.strip()]
    items: List[Item] = []
    for k, block in enumerate(blocks, 1):
        first = next((ln.strip().lstrip("#").strip() for ln in block.splitlines() if ln.strip()), stem)
        default_locator = f"{locator_prefix}#item={k}" if len(blocks) > 1 else locator_prefix
        meta = std_meta(block)
        if meta:
            fields = {key: val for key, val in meta.items() if key not in ("id", "source")}
            # 机器生成的文件总带块号:出问题时要能说清是中间文件的第几块
            fields["ingested_from"] = f"{locator_prefix}#item={k}"
            fields["_std"] = "1"
            items.append(Item(id=_std_item_id(meta, first, default_locator), title=first[:80],
                              text=block + "\n", locator=meta.get("source") or default_locator,
                              fields=fields))
            continue
        items.append(Item(id=stem if len(blocks) == 1 else f"{stem}-{k}", title=first[:80],
                          text=block + "\n", locator=default_locator))
    return items


# ---------------------------------------------------------------- 脱敏

def redact(text: str) -> Tuple[str, int]:
    count = 0
    def sub(pattern, repl, s):
        nonlocal count
        s2, n = pattern.subn(repl, s)
        count += n
        return s2
    out = sub(_EMAIL, "<邮箱>", text)
    out = sub(_IDCARD, "<证件号>", out)
    out = sub(_PHONE, "<手机号>", out)
    out = sub(_IPV4, "<IP>", out)
    return out, count


# ---------------------------------------------------------------- 落盘

_SAFE_ID = re.compile(r"[^0-9A-Za-z一-鿿_.-]+")
# 标准化块里允许进 item frontmatter 的键(其余留在正文里)
_STD_FRONT_KEYS = ("system", "occurred_at", "severity", "conclusion", "ingested_from")


def write_items(items_dir: pathlib.Path, items: Sequence[Item], source_name: str,
                do_redact: bool = False) -> List[pathlib.Path]:
    """每单一文件,frontmatter 记出处;同名覆盖(同一材料重导)。"""
    items_dir.mkdir(parents=True, exist_ok=True)
    written: List[pathlib.Path] = []
    used: Dict[str, int] = {}
    for it in items:
        safe = _SAFE_ID.sub("-", it.id).strip("-") or "item"
        # 同一批里撞名:加序号,不覆盖。覆盖就是无声地丢一单(review 时也对不上 item_id)。
        n = used.get(safe, 0)
        used[safe] = n + 1
        if n:
            safe = "%s-%d" % (safe, n + 1)
            it = Item(id="%s-%d" % (it.id, n + 1), title=it.title, text=it.text, locator=it.locator, fields=it.fields)
        body, n = redact(it.text) if do_redact else (it.text, 0)
        front = {"item_id": it.id, "title": it.title, "source": it.locator, "source_file": source_name,
                 "redacted": n}
        if (it.fields or {}).get("_std"):
            # 标准化材料:把 kb-init 已定的字段带进 frontmatter,导入侧据此预填工作单并核对候选。
            # 只搬这个白名单——表格那条路的 fields 是整行原始列,不能往 frontmatter 里倒。
            front.update({k: it.fields[k] for k in _STD_FRONT_KEYS if it.fields.get(k)})
        text = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()) + "\n---\n\n" + body
        path = items_dir / f"{safe}.md"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
