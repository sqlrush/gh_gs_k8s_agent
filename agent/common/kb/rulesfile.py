"""规则文件(rules/*.yaml、guides/*.md、errata/*.md)的读取辅助。

从 gaussdb-kb/scripts/kb.py 搬出:查询 skill 与导入 skill 都要读这些文件,而两个 skill 的
脚本目录在镜像里是物理分开的,只能在 common/ 里共用。函数体与原来逐字相同。
"""
from __future__ import annotations

import pathlib
import re

import yaml

KB_SUBDIRS = ("errata", "rules", "guides", "archive", "sources", "inbox",
              # 2026-10-24 八类知识:六类类型化条目各一个目录;topics/ 放 L1;evidence/ 放照片、日志等原始证据(L3)
              "wiki", "product", "runbooks", "defects", "baselines", "profiles", "topics", "evidence")
RULE_ID_RE = re.compile(r"^GS-[A-Z]{2,4}-\d{3}$")
SEVERITIES = frozenset({"error", "warn", "info"})
CHECK_KINDS = frozenset({"deterministic", "advisory"})
RULE_REQUIRED_FIELDS = ("id", "severity", "check", "rule")

# A clause is either in force or withdrawn. Legacy clauses have no `status` field
# at all, so its absence must mean active — see rule_status().
STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"
STATUSES = frozenset({STATUS_ACTIVE, STATUS_DEPRECATED})
# archive/ is scanned first on purpose: it is settled history, so when an ID
# collides it is the *new* clause that must be blamed, not the withdrawn one it
# collided with. Scan rules/ first and validate points the operator at archive/ —
# the exact opposite of the file they need to fix.
RULE_DIRS = ("archive", "rules")
SEARCH_HIT_CAP = 200

# The skills' grep range — archive/ is deliberately not in it.
SEARCHABLE = (("errata", (".md",)), ("rules", (".yaml", ".yml")), ("guides", (".md",)),
              # 2026-10-24 六类类型化条目(手册 / 缺陷 / 基线 / 产品文档 / Wiki / 画像)
              ("runbooks", (".md",)), ("defects", (".md",)), ("baselines", (".md",)),
              ("product", (".md",)), ("wiki", (".md",)), ("profiles", (".md",)))


def read_text_file(path: pathlib.Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def split_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """Return (meta, error). meta is {} when no frontmatter block exists."""
    if not text.startswith("---"):
        return {}, None
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not match:
        return None, "frontmatter 起始 --- 没有对应的结束 ---"
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, f"frontmatter YAML 解析失败:{exc}"
    if meta is None:
        return {}, None
    if not isinstance(meta, dict):
        return None, "frontmatter 不是键值映射"
    return meta, None


def load_rule_file(path: pathlib.Path) -> tuple[list, str | None]:
    try:
        data = yaml.safe_load(read_text_file(path))
    except (OSError, yaml.YAMLError) as exc:
        return [], f"YAML 解析失败:{exc}"
    if data is None:
        return [], None
    if not isinstance(data, list):
        return [], "顶层必须是条款列表(yaml list)"
    return data, None


def rule_status(entry: dict) -> str:
    """`active` unless the entry says otherwise.

    Legacy clauses were written before the field existed; treating a missing
    `status` as anything but active would retroactively withdraw the whole
    existing knowledge base.
    """
    return str(entry.get("status") or STATUS_ACTIVE).strip().lower()


def iter_files(kb: pathlib.Path, sub: str, suffixes: tuple[str, ...]) -> list[pathlib.Path]:
    root = kb / sub
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in suffixes)


def first_heading(path: pathlib.Path) -> str:
    try:
        text = read_text_file(path)
    except OSError:
        return path.stem
    meta, _ = split_frontmatter(text)
    if meta and meta.get("description"):
        return str(meta["description"])
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and not stripped.startswith("---"):
            return stripped
    return path.stem


def iter_active_rules(kb: pathlib.Path):
    """Yield (path, err, active_entries) for each rules/*.yaml, in file order.

    Deprecated clauses are dropped here — RULES.md is the judge-against list, and a
    withdrawn clause must never enter it (that is the whole point of archive/).
    """
    for path in iter_files(kb, "rules", (".yaml", ".yml")):
        entries, err = load_rule_file(path)
        if err:
            yield path, err, []
            continue
        active = [e for e in entries
                  if isinstance(e, dict) and rule_status(e) == STATUS_ACTIVE]
        yield path, None, active
