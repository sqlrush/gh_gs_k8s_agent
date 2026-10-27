#!/usr/bin/env python3
"""kb — build and govern the GaussDB spec knowledge base (kb).

Deterministic half of the KB pipeline. The model (per SKILL.md) does the
semantic half — classifying clauses into rules / guides / errata and drafting
entries. This script owns everything that must not depend on a model:

    ingest <file>       convert txt/md/docx/doc into <kb>/inbox/<slug>/source.md
                        (+ heading outline), snapshot the original into <kb>/sources/
    index               rebuild <kb>/INDEX.md from rules/*.yaml + guides/ + errata/
    validate            check encoding (non-UTF-8 is invisible to the skills' grep),
                        rule IDs, yaml schema, frontmatter, INDEX consistency,
                        and that withdrawn clauses actually left rules/
    search <keyword>    grep across errata/ rules/ guides/ (errata first)
    contract [--apply]  inject the KB-reference contract block into the judging skills
    cite-check          verify that case / clause IDs quoted in an answer exist (kb_cite.py)
    overview / topic / show / evidence / template   layered reads (kb_read.py)
    add / attach / retype / migrate / profile / taxonomy   typed items (kb_items.py)

Withdrawing a clause is a *move*, not a flag. The contract (references/kb-contract.md)
sends the skills at the KB with `grep -rn <kw> <kb>/errata <kb>/rules <kb>/guides`, and
grep prints only the matching *line* — a clause tagged `status: deprecated` still gets
hit on its `rule:` line, and the model never sees the tag. So a withdrawn clause moves
to <kb>/archive/, which is deliberately outside that grep range. `status` stays on the
entry so validate can enforce both halves: tagged-but-not-moved, and moved-but-not-tagged.
The ID is never deleted or reused — reports that cited it must stay traceable.

KB directory resolution (first hit wins):
    --kb DIR  >  $GSDB_KB_DIR  >  <install root>/kb

The KB sits *beside* the skills, never inside one — install-opencode.sh does
`rm -rf` on each skill directory, so a KB under skills/<name>/ would be wiped on
the next reinstall. Global install -> ~/.config/opencode/kb; project install ->
<project>/.opencode/kb. The customer configures nothing.

Exit codes: 0 = ok, 1 = runtime error, 2 = findings (validate errors, or
contract scan showing missing/stale blocks). Findings go to stdout.
"""
from __future__ import annotations

import argparse
import html
import os
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import date

try:
    import yaml
except ImportError:  # pragma: no cover
    print("缺少依赖 PyYAML:python3 -m pip install PyYAML", file=sys.stderr)
    sys.exit(1)

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))           # 兄弟模块 kb_store / kb_cases
for _anc in _HERE.parents:                       # common/(仓库根,或装好后的 skills/ 下)
    if (_anc / "common" / "kb" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

from common.kb.rulesfile import (  # noqa: E402,F401  —— 旧名字继续可用,kb_cases 与测试按 kb.<name> 取
    CHECK_KINDS, KB_SUBDIRS, RULE_DIRS, RULE_ID_RE, RULE_REQUIRED_FIELDS, SEARCH_HIT_CAP, SEARCHABLE,
    SEVERITIES, STATUS_ACTIVE, STATUS_DEPRECATED, STATUSES, first_heading, iter_active_rules, iter_files,
    load_rule_file, read_text_file, rule_status, split_frontmatter)
from common.kb import lock as kblock  # noqa: E402
from common.kb import store as spg  # noqa: E402
from common.kb.atomic import write_text_atomic  # noqa: E402

CONTRACT_BEGIN = "<!-- KB-CONTRACT:BEGIN"
CONTRACT_END = "<!-- KB-CONTRACT:END -->"
# 只有会做「规范判断 / 阈值判断」的 skill 才需要知识库契约。slowsql / topsql /
# sqlfetch / explain / topproc / procinfo 是纯取数(列表格、还原 SQL 文本),
# 要求它们「先查知识库再作答」只是噪音与延迟。
CONTRACT_SKILLS = frozenset({
    "sqlreview", "health", "wdr", "memanalyze", "sqltune", "proctune",
    "lockwait", "waitevent", "vacuum",        # 有 findings 的诊断 skill,同样按发现查库
})
# 部署仓的 skill 目录带 gaussdb- 前缀(gaussdb-health),主干不带;白名单按去前缀后的名字比。
SKILL_PREFIX = "gaussdb-"


def skill_base_name(name: str) -> str:
    return name[len(SKILL_PREFIX):] if name.startswith(SKILL_PREFIX) else name


HEADING_PATTERNS = (
    re.compile(r"^#{1,6}\s+\S"),
    re.compile(r"^第[零一二三四五六七八九十百0-9]+[章节部分篇]"),
    re.compile(r"^[一二三四五六七八九十]+、"),
    re.compile(r"^[((][一二三四五六七八九十0-9]+[))]"),
    re.compile(r"^\d+(?:\.\d+){0,3}[..、\s]\s*\S"),
)
MAX_HEADING_LEN = 60


class KbError(Exception):
    """Operator-facing failure; message is printed as-is."""


# ---------------------------------------------------------------- kb layout

def install_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """The directory the skills were installed into.

        ~/.config/opencode/skills/kb/scripts/kb.py -> ~/.config/opencode
        <项目>/.opencode/skills/.../kb.py                 -> <项目>/.opencode
        <仓库>/skills/.../kb.py                           -> <仓库>
    """
    here = (start or _HERE).resolve()
    for anc in here.parents:
        if anc.name == "skills":
            return anc.parent
    return here.parent.parent.parent          # 非标准布局:退回三级


def resolve_kb_dir(cli_value: str | None) -> pathlib.Path:
    """--kb > $GSDB_KB_DIR > <安装根>/kb

    The KB lives *next to* the skills, never inside one: install-opencode.sh
    does `rm -rf` on each skill directory, so a KB under skills/<name>/ would be
    destroyed on the next reinstall. A sibling of skills/ is never touched.
    """
    if cli_value:
        return pathlib.Path(cli_value).expanduser()
    env_dir = os.environ.get("GSDB_KB_DIR")
    if env_dir:
        return pathlib.Path(env_dir).expanduser()
    return install_root() / "kb"


def ensure_kb_skeleton(kb: pathlib.Path) -> None:
    for sub in KB_SUBDIRS:
        (kb / sub).mkdir(parents=True, exist_ok=True)
    version = kb / "VERSION"
    if not version.exists():
        version.write_text(date.today().strftime("%Y.%m") + "\n", encoding="utf-8")


def read_version(kb: pathlib.Path) -> str:
    try:
        return (kb / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


# ---------------------------------------------------------------- ingest

# 抽文本(txt/md/docx/doc/pdf)与扫描件/乱码闸门已搬到 common/kb/extract.py —— 标准化 skill
# (gaussdb-kb-init)走同一条路,两份实现改一处漏一处的风险不划算。这里按旧名字重导出,
# 并把 ExtractError 翻成 KbError:对外行为(函数名、消息、异常类型)一字不变。
from common.kb import extract as _extract  # noqa: E402
from common.kb.extract import (  # noqa: E402,F401  —— 旧名字继续可用,测试按 kb.<name> 取
    pdf_extraction_problem, pdf_page_count, normalize_text,
)


def _extracted(fn, path: pathlib.Path) -> str:
    try:
        return fn(path)
    except _extract.ExtractError as exc:
        raise KbError(str(exc)) from exc


def extract_docx(path: pathlib.Path) -> str:
    return _extracted(_extract.extract_docx, path)


def extract_doc(path: pathlib.Path) -> str:
    return _extracted(_extract.extract_doc, path)


def extract_pdf(path: pathlib.Path) -> str:
    return _extracted(_extract.extract_pdf, path)


def extract_source(path: pathlib.Path) -> str:
    return _extracted(_extract.extract_source, path)


def detect_outline(text: str) -> list[tuple[int, str]]:
    outline = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or len(stripped) > MAX_HEADING_LEN:
            continue
        if any(pat.match(stripped) for pat in HEADING_PATTERNS):
            outline.append((lineno, stripped))
    return outline


def slugify(stem: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", stem).strip("-").lower()
    return slug or "imported"


def unique_path(target: pathlib.Path) -> pathlib.Path:
    if not target.exists():
        return target
    for n in range(2, 100):
        candidate = target.with_name(f"{target.stem}-{n}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise KbError(f"同名文件过多,无法为 {target.name} 分配快照名")


SCAN_STUB = """<!-- 扫描件转录稿(extracted_by: model)。原件 {snapshot},页面图片在 {pages}/(page-01.jpg 起,{note})。
     逐页用 read 工具看图,把正文按原样转录到这段说明下面:保留章节编号、条款编号与表格(写成 markdown 表格);
     看不清的字写「[看不清]」并在最后集中问用户,**不要猜** —— 猜出来的内容进了知识库,会被当成贵方的规范。
     全部转录完删掉这段说明,再按常规条款化。 -->
"""


def _ingest_scanned(kb: pathlib.Path, src: pathlib.Path, snapshot: pathlib.Path, inbox: pathlib.Path, why) -> int:
    """扫描件 / 乱码 PDF:每页渲染成 jpg,source.md 先写转录说明,退出码 2(还要模型看图转录)。"""
    from common.kb import pdfpages
    try:
        rendered = pdfpages.render_pages(snapshot, inbox / "pages")
    except _extract.ExtractError as exc:
        raise KbError(f"{why}\n(渲染成图片也没成功:{exc})") from exc
    inbox.mkdir(parents=True, exist_ok=True)
    pages_rel = (inbox / "pages").relative_to(kb).as_posix()
    (inbox / "source.md").write_text(
        SCAN_STUB.format(snapshot=snapshot.relative_to(kb).as_posix(), pages=pages_rel, note=rendered.note()),
        encoding="utf-8")
    (inbox / "outline.md").write_text(f"# outline of {src.name}\n\n(扫描件:转录后再看标题)\n", encoding="utf-8")
    kind = "乱码(字体缺映射)" if getattr(why, "reason", "") == "garbled" else "扫描件(字是图片)"
    print(f"KB 目录        : {kb}")
    print(f"原始快照       : {snapshot.relative_to(kb)}")
    print(f"文字层不可用   : {kind} —— 已把每页渲染成图片,由你看图转录")
    print(f"页面图片       : {pages_rel}/({rendered.note()})")
    print(f"转录写到       : {(inbox / 'source.md').relative_to(kb)}(里面有转录要求)")
    print()
    print("Next: 逐页 read 页面图片,把正文转录进 source.md(看不清的写「[看不清]」、集中问用户,不要猜);"
          "转录完再按 SKILL.md 工作流条款化。转录内容来自看图,条款的 source 写明「扫描件转录」。")
    return 2


def cmd_ingest(args: argparse.Namespace) -> int:
    src = pathlib.Path(args.file).expanduser()
    if not src.is_file():
        # 现场:用户给的是自己电脑上的路径(D:\…)。skill 在沙箱里读不到,要说清「上传到沙箱哪个目录、命令怎么写」,
        # 并按文件名在沙箱常见目录找一遍同名文件——前端可能已经传上来了。
        from common.kb import inbox as kbinbox
        raise KbError(kbinbox.missing_file_message(args.file, resolve_kb_dir(args.kb)))
    kb = resolve_kb_dir(args.kb)
    ensure_kb_skeleton(kb)

    unreadable = None
    try:
        text = normalize_text(_extract.extract_source(src))
    except _extract.UnreadablePdf as exc:      # 扫描件 / 乱码 PDF:渲染成图片,由模型看图转录(下面)
        unreadable, text = exc, ""
    except _extract.ExtractError as exc:
        raise KbError(str(exc)) from exc
    outline = detect_outline(text) if text else []

    snapshot = unique_path(kb / "sources" / src.name)
    shutil.copy2(src, snapshot)

    inbox = kb / "inbox" / slugify(src.stem)
    if unreadable is not None:
        return _ingest_scanned(kb, src, snapshot, inbox, unreadable)
    # Same filename -> same slug -> source.md is overwritten. That is the right
    # default (same name means a new edition of the same document, and the latest
    # is what you want), but doing it *silently* is not: whatever was still waiting
    # to be turned into clauses just vanished, with nothing in the output to say so.
    # The original is still snapshotted in sources/, so nothing is actually lost —
    # only the pending work. Say that much.
    superseded = 0
    if (inbox / "source.md").is_file():
        superseded = read_text_file(inbox / "source.md").count("\n")
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "source.md").write_text(text, encoding="utf-8")
    outline_lines = [f"- L{lineno}: {title}" for lineno, title in outline]
    (inbox / "outline.md").write_text(
        f"# outline of {src.name}(自动探测,仅供分段参考)\n\n"
        + ("\n".join(outline_lines) + "\n" if outline_lines else "(未探测到标题)\n"),
        encoding="utf-8",
    )

    total_lines = text.count("\n")
    print(f"KB 目录        : {kb}")
    print(f"原始快照       : {snapshot.relative_to(kb)}")
    print(f"待条款化文本   : {(inbox / 'source.md').relative_to(kb)}({total_lines} 行)")
    print(f"标题大纲       : {(inbox / 'outline.md').relative_to(kb)}({len(outline)} 个候选标题)")
    if src.suffix.lower() == ".pptx":
        try:
            media = _extract.pptx_media(src, inbox / "media")
        except (_extract.ExtractError, OSError) as exc:
            media = []
            print(f"幻灯片图片     : 没取出来({exc});不影响文字部分")
        if media:
            print(f"幻灯片图片     : {len(media)} 张 → {(inbox / 'media').relative_to(kb)}/(slideNN-*,"
                  f"截图、架构图、监控曲线需要时用 read 工具看图,把要点补进对应页)")

    if superseded:
        print()
        print(f"⚠ 覆盖:inbox/{inbox.name}/ 里原有上次导入、**尚未条款化**的内容"
              f"({superseded} 行),已被本次的新版覆盖。")
        print(f"  上一版原文快照仍在 sources/ 下(未删除),需要时可重新导入。")

    existing = sum(len(load_rule_file(p)[0])
                   for p in iter_files(kb, "rules", (".yaml", ".yml")))
    if existing:
        # The script cannot tell which clauses the new edition dropped — that is a
        # semantic diff. What it *can* do deterministically is refuse to let a
        # re-import look like a first import, which is how stale clauses survive.
        print()
        print(f"⚠ 换版导入:知识库里已有 {existing} 条现行条款。")
        print("  条款化前必须先读 INDEX.md,把新版原文与现有条款逐条比对:"
              "新增 / 沿用 / 修改 / 废止。")
        print("  废止的条款移进 archive/ 并标 status: deprecated —— **不要直接删除**"
              "(ID 需永远可追溯,且永不复用)。")
        print("  漏掉这一步,各 skill 会继续按已废止的规范判客户违规,而 validate 查不出来。")
        print()

    print("Next: 按 SKILL.md 工作流分段阅读 source.md,把条款分类写入 rules/ guides/ errata/,"
          "然后运行 index 与 validate。")
    return 0


# ---------------------------------------------------------------- shared parsing

# split_frontmatter / load_rule_file / rule_status / iter_files / first_heading:
# 已搬到 common/kb/rulesfile.py(查询 skill 与导入 skill 共用),顶部按原名导入。


# ---------------------------------------------------------------- index

def describe_rule_file(path: pathlib.Path) -> str:
    entries, err = load_rule_file(path)
    if err:
        return f"⚠ {err}"
    ids = [e.get("id", "?") for e in entries if isinstance(e, dict)]
    span = f"{ids[0]}..{ids[-1]}" if ids else "空"
    comment = ""
    try:
        head = read_text_file(path).splitlines()[0]
        if head.startswith("#"):
            comment = ":" + head.lstrip("#").strip()
    except (OSError, IndexError):
        pass
    return f"{len(ids)} 条({span}){comment}"


# RULES.md is the rule-level L1 index: one line per *active* clause, always loaded
# by the consuming skills so the model picks by reading the full set instead of
# guessing what keyword to grep. INDEX.md stops at the file — the semantic-drift
# gap lives in the file→clause jump, and this is what closes it.
_LISTED_ID_RE = re.compile(r"`(GS-[A-Z]{2,4}-\d{3})`")


def _rule_file_comment(path: pathlib.Path) -> str:
    try:
        head = read_text_file(path).splitlines()[0]
    except (OSError, IndexError):
        return ""
    return head.lstrip("#").strip() if head.startswith("#") else ""


def render_rules_listing(kb: pathlib.Path) -> str:
    total = 0
    body: list[str] = []
    for path, err, active in iter_active_rules(kb):
        rel = path.relative_to(kb / "rules")
        comment = _rule_file_comment(path)
        body += ["", f"## rules/{rel}" + (f" — {comment}" if comment else ""), ""]
        if err:
            body.append(f"- ⚠ {err}(修好后重跑 index)")
            continue
        for entry in active:
            rid = entry.get("id", "?")
            sev = entry.get("severity", "?")
            rule = str(entry.get("rule", "")).strip() or "(空条款)"
            body.append(f"- `{rid}` · {sev} · {rule}")
            total += 1

    header = [
        "# 现行条款速查(RULES)",
        "",
        f"> 由 `kb.py index` 自动生成,勿手工编辑。版本 {read_version(kb)} · "
        f"现行条款 {total} 条。",
        "> **这是全部现行条款的逐条清单。** 判定前先在此逐条判断相关性,"
        "再去 `rules/` 读选中条款全文(rationale/criteria/keywords)。",
        "> 不必猜关键词 —— 条款都在这里。已废止条款不在此列"
        "(见 `INDEX.md` 的 archive 段,仅供 ID 追溯,**不得据此判定**)。",
    ]
    return "\n".join(header + (body or ["", "(暂无现行条款)"])) + "\n"


def render_cases_listing(kb: pathlib.Path) -> str:
    """CASES.md:案例的逐条清单,与 RULES.md 同级。文件模式(没配向量库/图库)下模型靠它把案例当 md
    知识库加载:先扫清单挑相关案例,再读 cases/ 全文。坏文件以 ⚠ 露头,不静默少一条。"""
    from common.kb import cases as kbcases
    cases, findings = kbcases.load_cases(kb)
    header = [
        "# 案例速查(CASES)",
        "",
        f"> 由 `kb.py index` 自动生成,勿手工编辑。版本 {read_version(kb)} · 案例 {len(cases)} 条。",
        "> **这是全部案例的逐条清单。** 没配向量库/图库(文件模式)时,先在此按现象 / 对象 / 复发标志挑相关案例,",
        "> 再读 `cases/` 里的全文(现场 / 判断 / 处置 / 复发标志)。引用必带案例 ID;结论强度不是「已确认」的要带上标签。",
        "",
    ]
    body: list[str] = []
    for c in sorted(cases, key=lambda c: (c.occurred_at, c.id), reverse=True):
        try:
            rel = c.path.relative_to(kb).as_posix()
        except ValueError:
            rel = f"cases/{c.path.name}"
        signals = f" · 复发标志:{';'.join(c.signals)}" if c.signals else ""
        body.append(f"- `{c.id}` · {c.system} · {c.occurred_at} · {c.conclusion} · {c.title}{signals} · `{rel}`")
    body += [f"- ⚠ {m}(修好后重跑 index)" for lvl, m in findings if lvl == "error"]
    return "\n".join(header + (body or ["(暂无案例)"])) + "\n"


def first_heading_typed(path: pathlib.Path) -> str:
    """类型化条目:INDEX 里写「ID · 标题」。"""
    try:
        meta, _ = split_frontmatter(read_text_file(path))
    except OSError:
        meta = None
    if meta and meta.get("title"):
        return f"{meta.get('id', path.stem)} · {meta['title']}"
    return first_heading(path)


def cmd_index(args: argparse.Namespace) -> int:
    kb = resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise KbError(f"KB 目录不存在:{kb}(先运行 ingest 或手工创建)")
    ensure_kb_skeleton(kb)

    errata = iter_files(kb, "errata", (".md",))
    rules = iter_files(kb, "rules", (".yaml", ".yml"))
    guides = iter_files(kb, "guides", (".md",))
    archive = iter_files(kb, "archive", (".yaml", ".yml"))
    rule_total = sum(len(load_rule_file(p)[0]) for p in rules)
    archive_total = sum(len(load_rule_file(p)[0]) for p in archive)

    lines = [
        "# 规范知识库索引(INDEX)",
        "",
        f"> 由 `kb.py index` 自动生成,勿手工编辑。版本 {read_version(kb)} · "
        f"勘误 {len(errata)} 篇 · 条款 {rule_total} 条 · 指南 {len(guides)} 篇 · "
        f"已废止 {archive_total} 条。",
        "> 查询优先级:errata/ > rules/ > guides/ > 模型自带知识。",
        "> **现行条款的逐条清单见 `RULES.md`**(判定前先读它);**案例的逐条清单见 `CASES.md`**"
        "(查历史相似先读它);本索引到文件级为止。",
        "",
        "## errata/(修正与例外 —— 最高优先级)",
        "",
    ]
    lines += [f"- `errata/{p.relative_to(kb / 'errata')}` — {first_heading(p)}" for p in errata] or ["(暂无)"]
    lines += ["", "## rules/(机器可判定条款,稳定 GS-* ID)", ""]
    lines += [f"- `rules/{p.relative_to(kb / 'rules')}` — {describe_rule_file(p)}" for p in rules] or ["(暂无)"]
    lines += ["", "## guides/(语义指南,模型判断依据)", ""]
    lines += [f"- `guides/{p.relative_to(kb / 'guides')}` — {first_heading(p)}" for p in guides] or ["(暂无)"]
    from common.kb import types as kbtypes
    for kt in kbtypes.TYPED.values():
        files = iter_files(kb, kt.dir, (".md",))
        lines += ["", f"## {kt.dir}/({kt.name})", ""]
        lines += [f"- `{kt.dir}/{p.relative_to(kb / kt.dir)}` — {first_heading_typed(p)}" for p in files] or ["(暂无)"]
    lines += [
        "",
        "## archive/(已废止条款 —— 仅供追溯,**不得据此判定**)",
        "",
        "> 这些条款已被客户的新版规范废止,**不在检索范围内**,也不得用来认定任何违规。",
        "> 列在这里只为一件事:历史报告引用过这些 ID,ID 必须永远可追溯、且永不复用。",
        "",
    ]
    lines += [f"- `archive/{p.relative_to(kb / 'archive')}` — {describe_rule_file(p)}" for p in archive] or ["(暂无)"]
    lines += ["", "定位方式:先按本索引选文件,再 `grep -rn \"<关键词>\" <kb>/{errata,rules,guides}/`"
              "(archive/ 不在其中,是有意为之)。", ""]

    # 三份清单是 runtime 每次检索都读的入口:先写临时文件再改名,读的一方永远拿到完整文件
    write_text_atomic(kb / "INDEX.md", "\n".join(lines))
    listing = render_rules_listing(kb)
    write_text_atomic(kb / "RULES.md", listing)
    cases_listing = render_cases_listing(kb)
    write_text_atomic(kb / "CASES.md", cases_listing)
    case_total = sum(1 for ln in cases_listing.splitlines() if ln.startswith("- `"))
    print(f"INDEX.md 已重建:{kb / 'INDEX.md'}({len(lines)} 行);"
          f"RULES.md 已重建:{kb / 'RULES.md'}({rule_total} 条现行条款);"
          f"CASES.md 已重建:{kb / 'CASES.md'}({case_total} 条案例)")
    from common.kb import layers as kblayers
    written, warns = kblayers.write_layers(kb, read_version(kb))
    print(f"分层已重建  : L0-总索引.md + {len(written) - 1} 个专题导航(topics/*.md)")
    for w in warns:
        print(f"[warn ] {w}")
    return 0


# ---------------------------------------------------------------- validate

def _duplicate_id_msg(where: str, rid: str, first_seen: str) -> str:
    """Name the offender, and say which kind of collision it is."""
    if first_seen.startswith("archive/"):
        return (f"{where}: ID `{rid}` 复用了 archive/ 中**已废止**条款的 ID"
                f"({first_seen})—— ID 永不复用:历史报告引用过它,复用会让追溯指向"
                f"一条完全不同的条款。请分配新号。")
    return f"{where}: ID `{rid}` 与 {first_seen} 重复"


def _validate_placement(where: str, rid: str, sub: str, entry: dict,
                        findings: list) -> None:
    """A withdrawn clause must be in archive/, and archive/ must hold only those.

    Both halves are silent failures if unchecked. Tagged-but-not-moved: the skills
    grep rules/ and keep hitting the clause's `rule:` line, never seeing the tag —
    they go on judging the customer against a spec that was withdrawn. The reverse,
    moved-but-not-tagged, quietly drops a clause that is still in force out of the
    searchable range. Neither shows up as an error anywhere else.
    """
    status = rule_status(entry)
    if status not in STATUSES:
        findings.append((
            "error",
            f"{where}: status `{entry.get('status')}` 非法,必须是 {sorted(STATUSES)}"))
        return

    if sub == "rules" and status == STATUS_DEPRECATED:
        findings.append((
            "error",
            f"{where}: 条款 `{rid}` 标了 status: deprecated 却仍留在 rules/ —— "
            f"各 skill 用 `grep -rn` 检索 rules/,只看得到命中行、看不到 status,"
            f"**照样会命中它**并按已废止的规范判定。请移到 archive/(ID 保留,不要删除)"))
    elif sub == "archive" and status != STATUS_DEPRECATED:
        findings.append((
            "error",
            f"{where}: 条款 `{rid}` 放在 archive/ 却不是 status: deprecated —— "
            f"archive/ 不在检索范围内,一条现行条款搁在这里等于被静默弃用。"
            f"要么补 status: deprecated,要么移回 rules/"))
    elif sub == "archive" and not entry.get("superseded_by"):
        findings.append((
            "warn",
            f"{where}: 废止条款建议填写 superseded_by(被哪条取代,或为何废止),"
            f"否则日后无法追溯当初为什么下架"))


def validate_rules(kb: pathlib.Path, findings: list) -> None:
    seen: dict[str, str] = {}
    for sub in RULE_DIRS:                    # ID 唯一性必须跨目录:废止的 ID 也不得复用
        for path in iter_files(kb, sub, (".yaml", ".yml")):
            rel = path.relative_to(kb)
            entries, err = load_rule_file(path)
            if err:
                findings.append(("error", f"{rel}: {err}"))
                continue
            for i, entry in enumerate(entries):
                where = f"{rel}[{i}]"
                if not isinstance(entry, dict):
                    findings.append(("error", f"{where}: 条款必须是键值映射"))
                    continue
                missing = [f for f in RULE_REQUIRED_FIELDS if not entry.get(f)]
                if missing:
                    findings.append(("error", f"{where}: 缺少必填字段 {missing}"))
                rid = str(entry.get("id", ""))
                if rid and not RULE_ID_RE.match(rid):
                    findings.append(("error", f"{where}: ID `{rid}` 不符合 GS-<域>-NNN 格式"))
                if rid in seen:
                    findings.append(("error", _duplicate_id_msg(where, rid, seen[rid])))
                elif rid:
                    seen[rid] = str(where)
                if entry.get("severity") not in SEVERITIES:
                    findings.append(("error", f"{where}: severity 必须是 {sorted(SEVERITIES)}"))
                if entry.get("check") not in CHECK_KINDS:
                    findings.append(("error", f"{where}: check 必须是 {sorted(CHECK_KINDS)}"))
                if entry.get("check") == "advisory" and not entry.get("criteria"):
                    findings.append(("warn", f"{where}: advisory 条款建议提供 criteria(判定依据)"))
                if not entry.get("source"):
                    findings.append(("warn", f"{where}: 建议填写 source(原文出处),保证可追溯"))
                _validate_placement(where, rid, sub, entry, findings)


def validate_guides(kb: pathlib.Path, findings: list) -> None:
    seen: dict[str, str] = {}
    for path in iter_files(kb, "guides", (".md",)):
        rel = str(path.relative_to(kb))
        try:
            text = read_text_file(path)
        except OSError as exc:
            findings.append(("error", f"{rel}: 读取失败:{exc}"))
            continue
        meta, err = split_frontmatter(text)
        if err:
            findings.append(("error", f"{rel}: {err}"))
            continue
        if not meta:
            findings.append(("error", f"{rel}: 缺少 frontmatter(需要 id 与 description)"))
            continue
        for field in ("id", "description"):
            if not meta.get(field):
                findings.append(("error", f"{rel}: frontmatter 缺少 {field}"))
        gid = str(meta.get("id", ""))
        if gid and gid in seen:
            findings.append(("error", f"{rel}: guide id `{gid}` 与 {seen[gid]} 重复"))
        elif gid:
            seen[gid] = rel


# Directories the consuming skills grep. MUST stay in step with the grep range in
# references/kb-contract.md. archive/ is absent on purpose — that omission is the
# entire mechanism by which a withdrawn clause stops reaching the skills.
# sources/ is excluded too: it holds the customer's original files and keeps
# whatever encoding they arrived in; nothing greps it.
# 定义在 common/kb/rulesfile.SEARCHABLE(查询 skill 也要用同一份范围)。
_SEARCHABLE = SEARCHABLE

# archive/ is out of the grep range but validate still parses it, so it has to be
# UTF-8 like the rest — a GB18030 archive file would make ID-reuse checks miss it.
_ENCODED = _SEARCHABLE + (("archive", (".yaml", ".yml")),)


def validate_encoding(kb: pathlib.Path, findings: list) -> None:
    """Non-UTF-8 files in the searchable dirs are a silent trap.

    The consuming skills locate clauses with `grep -rn "<关键词>" <kb>/...`, per the
    contract block. grep compares the LLM's UTF-8 bytes against the file's bytes —
    against a GB18030 file that simply does not match, and grep does not complain:
    it just finds nothing. The model then answers「知识库未覆盖,以下为通用经验」
    even though the customer's rule *is* in the KB — exactly the lie this skill
    exists to prevent. Catch it at import time, loudly.

    (kb's own `search` decodes gb18030 and would find the file, which is
    what makes the failure so easy to miss during testing.)
    """
    for sub, suffixes in _ENCODED:
        for path in iter_files(kb, sub, suffixes):
            try:
                path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                findings.append((
                    "error",
                    f"{path.relative_to(kb)}: 不是 UTF-8 编码 —— 各 skill 用 "
                    f"grep 检索知识库时会**静默漏掉**这个文件(不报错,只是搜不到),"
                    f"导致模型误以为「知识库未覆盖」。请转存为 UTF-8:"
                    f"`iconv -f gb18030 -t utf-8 {path.name} > {path.name}.utf8 "
                    f"&& mv {path.name}.utf8 {path.name}`"
                ))


def validate_index(kb: pathlib.Path, findings: list) -> None:
    index = kb / "INDEX.md"
    if not index.is_file():
        findings.append(("error", "INDEX.md 不存在(运行 kb.py index)"))
        return
    index_text = read_text_file(index)
    # archive/ is indexed too (its IDs must stay traceable), so it is checked here
    # as well — otherwise deleting an archived file leaves a dangling INDEX entry
    # that nothing complains about.
    kb_files = [p for sub, suffixes in _ENCODED for p in iter_files(kb, sub, suffixes)]
    newest = 0.0
    for path in kb_files:
        rel = str(path.relative_to(kb))
        newest = max(newest, path.stat().st_mtime)
        if rel not in index_text:
            findings.append(("error", f"INDEX.md 缺少条目:{rel}(重新运行 index)"))
    for rel in re.findall(r"`((?:errata|rules|guides|archive|runbooks|defects|baselines|product|wiki|profiles)/[^`]+)`", index_text):
        if not (kb / rel).is_file():
            findings.append(("error", f"INDEX.md 引用了不存在的文件:{rel}"))
    if kb_files and index.stat().st_mtime < newest:
        findings.append(("warn", "INDEX.md 比库内容旧(重新运行 index)"))


def validate_rules_listing(kb: pathlib.Path, findings: list) -> None:
    """RULES.md 必须与 rules/ 的现行条款**逐一对齐**,两个方向都当 error。

    这一层引入了一个新的静默失效点:RULES.md 是模型判定时逐条筛选的候选清单,
    清单漏掉一条,那条条款对模型就等同于不存在 —— 跟 grep 没命中一样,不报错、查不出。
    所以校验双向卡死:
      - 现行条款缺席清单 → 模型永远看不见它;
      - 清单里的 ID 在 rules/ 已无对应现行条款 → 幽灵条款(多半是废止/删除后忘了重跑 index),
        会让模型据一条并不存在的规范去判客户违规。
    """
    listing = kb / "RULES.md"
    if not listing.is_file():
        findings.append(("error", "RULES.md 不存在(运行 kb.py index 生成现行条款清单)"))
        return
    listed = set(_LISTED_ID_RE.findall(read_text_file(listing)))

    active: set[str] = set()
    newest = 0.0
    for path, err, entries in iter_active_rules(kb):
        newest = max(newest, path.stat().st_mtime)
        if err:
            continue                      # 坏文件由 validate_rules 另行报错,这里不重复
        active.update(str(e["id"]) for e in entries if e.get("id"))

    for rid in sorted(active - listed):
        findings.append((
            "error",
            f"RULES.md 缺少现行条款 `{rid}` —— 模型逐条筛选时看不到它,"
            f"等同于这条规范不存在(重新运行 index)"))
    for rid in sorted(listed - active):
        findings.append((
            "error",
            f"RULES.md 列出了 `{rid}`,但 rules/ 里已无此现行条款 —— 幽灵条款会让模型"
            f"据一条不存在的规范判违规(废止/删除后重新运行 index)"))
    if active and listing.stat().st_mtime < newest:
        findings.append(("warn", "RULES.md 比 rules/ 旧(重新运行 index)"))


def validate_cases_and_graph(kb: pathlib.Path, findings: list) -> None:
    """案例文件与三元组文件:schema、结论强度、出处、ID 唯一、边的端点/置信度/状态。"""
    from common.kb import cases as kbcases
    from common.kb import graphfiles as gf

    cases, case_findings = kbcases.load_cases(kb)
    findings.extend(case_findings)
    triples, tri_findings = gf.load_triples(kb, case_ids=[c.id for c in cases])
    findings.extend(tri_findings)
    validate_symptom_reachability(triples, findings)


def validate_typed(kb: pathlib.Path, findings: list) -> None:
    """八类知识:类型化条目的 schema、ID 全库唯一、专题合法、条款的审批 / 生效版本、反例的区分证据、L0 新旧。"""
    from common.kb import catalog as kbcat
    from common.kb import graphfiles as gf
    from common.kb import layers as kblayers
    from common.kb import types as kbtypes

    _items, item_findings = kbtypes.load_items(kb)
    findings.extend(item_findings)
    cat = kbcat.load(kb, with_docsets=False)
    findings.extend(("warn", w) for w in cat.warnings)
    owner: dict[str, str] = {}
    for e in cat.entries:
        for key in (e.id,) + tuple(e.aliases):
            if key in owner and owner[key] != e.file:
                findings.append(("error", f"{e.file}: ID `{key}` 与 {owner[key]} 重复(ID 全库唯一,改过类型的旧 ID 也算)"))
            owner.setdefault(key, e.file)
    valid = {t.slug for t in cat.topics}
    for e in cat.entries:
        bad = [t for t in e.topics if t not in valid] if not e.topics_inferred else []
        if bad:
            findings.append(("warn", f"{e.file}: {e.id} 的专题 {bad} 不在专题定义里(kb.py topic --list)"))
    norms = cat.of_type(kbtypes.NORM)
    lacking = [e.id for e in norms if not (e.meta.get("approval") or e.meta.get("effective"))]
    if norms and lacking:
        findings.append(("warn", f"{len(lacking)}/{len(norms)} 条条款没写 approval(审批与变更要求)/ effective(生效版本),"
                                 f"如 {', '.join(lacking[:3])}——公司内部规范要记这两项"))
    guides = cat.of_type(kbtypes.GUIDE)
    if guides:
        findings.append(("warn", f"guides/ 里还有 {len(guides)} 篇旧指南没归类型(kb.py migrate 出清单,确认后 --apply)"))
    untagged = [e.id for e in cat.entries if not e.archived and e.type != "profile" and not e.topics]
    if untagged:
        findings.append(("warn", f"{len(untagged)} 条没归进任何专题(L0 里列为「未归类」),如 {', '.join(untagged[:3])}"))
    triples, _ = gf.load_triples(kb, case_ids=[e.id for e in cat.of_type(kbtypes.CASE)])
    reported = set()
    for t in cat.topics:
        view = kblayers.topic_view(cat, triples, t.slug)
        for c in view.causes:
            key = (c.symptom, c.rootcause)
            if c.symptom in view.lookalikes and c.disc_from != "区分证据" and key not in reported:
                reported.add(key)
                findings.append(("warn", f"现象「{c.symptom}」在本行有多种根因,根因「{c.rootcause}」的案例"
                                         f" {', '.join(c.cases[:2])} 没写「区分证据」——看起来相同、根因不同的反例要记下区分它们的证据"))
    l0 = kb / kblayers.L0_FILE
    newest = max((p.stat().st_mtime for _c, p in kbtypes.iter_item_files(kb)), default=0.0)
    if not l0.is_file():
        findings.append(("warn", "L0-总索引.md 不存在(运行 kb.py index 生成 L0 / L1)"))
    elif newest and l0.stat().st_mtime < newest:
        findings.append(("warn", "L0-总索引.md 比条目旧(重新运行 index)"))


def validate_symptom_reachability(triples, findings: list) -> None:
    """现象节点断成两半是静默失效:案例 `exhibits` 的现象与因果链起点的现象没归一,
    这个案例就永远走不到自己的 现象→根因→处置 链。向量检索靠语义相似能绕过去,
    纯词法(文件模式 / 没配 embedding)绕不过去——不报错,只是「路径:无」。"""
    exhibited: dict[str, str] = {}      # 被案例指到的现象 id → 名字
    chained: dict[str, str] = {}        # 有 caused_by 出边的现象 id → 名字
    for t in triples:
        if t.status == "rejected":
            continue
        if t.rel == "exhibits" and t.dst.kind == "symptom":
            exhibited.setdefault(t.dst.id, t.dst.name)
        elif t.rel == "caused_by" and t.src.kind == "symptom":
            chained.setdefault(t.src.id, t.src.name)
    orphan = sorted(set(exhibited) - set(chained))
    if not orphan or not chained:
        return
    for sid in orphan:
        findings.append((
            "warn",
            f"graph: 现象 `{exhibited[sid]}`({sid})只有案例 exhibits 指向它,没有 caused_by 出边——"
            f"引用这个现象的案例走不到「现象→根因→处置」路径。库里另有 {len(chained)} 个带因果链的现象"
            f"(如 `{chained[sorted(chained)[0]]}`):同一个现象的不同叫法请在 graph/canonical.yaml 里归一到同一个 id,"
            f"否则纯词法检索(文件模式 / 未配 embedding)查不到这条路径"))


def cmd_validate(args: argparse.Namespace) -> int:
    kb = resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise KbError(f"KB 目录不存在:{kb}")
    findings: list[tuple[str, str]] = []
    validate_encoding(kb, findings)      # 先查编码:非 UTF-8 会被 grep 静默漏掉
    validate_rules(kb, findings)
    validate_guides(kb, findings)
    validate_index(kb, findings)
    validate_rules_listing(kb, findings)
    validate_cases_and_graph(kb, findings)
    validate_typed(kb, findings)
    for sub in sorted((kb / "inbox").glob("*")) if (kb / "inbox").is_dir() else []:
        if not sub.is_dir():
            continue
        if sub.name in ("uploads", "drafts"):            # 收件目录与类型化条目草稿,不是待条款化的规范
            continue
        if sub.name == "clips":                         # 网页剪藏:每个批次一个目录,没转成 md 的才提醒
            for clip in sorted(sub.glob("clip-*")):
                if (clip / "meta.json").is_file() and not (clip / "page.md").is_file():
                    findings.append(("warn", f"inbox/clips/{clip.name} 剪藏了还没导入(kb.py clip ingest {clip.name})"))
            continue
        if (sub / "manifest.json").is_file():          # 工单批次,不是规范
            items = list((sub / "items").glob("*.md")) if (sub / "items").is_dir() else []
            if items:
                findings.append(("warn", f"inbox/{sub.name} 还有 {len(items)} 单工单未处理(propose → review → apply)"))
            continue
        src_md = sub / "source.md"
        if src_md.is_file() and read_text_file(src_md).startswith("<!-- 扫描件转录稿"):
            findings.append(("warn", f"inbox/{sub.name} 是扫描件,**还没转录**(逐页看 pages/ 的图,转录进 source.md 后再条款化)"))
            continue
        findings.append(("warn", f"inbox/{sub.name} 尚未条款化(处理完后 kb.py inbox-clean {sub.name})"))

    errors = [m for lvl, m in findings if lvl == "error"]
    warns = [m for lvl, m in findings if lvl == "warn"]
    for msg in errors:
        print(f"[error] {msg}")
    for msg in warns:
        print(f"[warn ] {msg}")
    print(f"validate: {len(errors)} error, {len(warns)} warn(KB={kb},版本 {read_version(kb)})")
    return 2 if errors else 0


# ---------------------------------------------------------------- contract

def load_contract_template() -> str:
    tpl = _HERE.parent.parent / "references" / "kb-contract.md"
    if not tpl.is_file():
        raise KbError(f"契约模板不存在:{tpl}")
    text = read_text_file(tpl).strip() + "\n"
    if CONTRACT_BEGIN not in text or CONTRACT_END not in text:
        raise KbError("契约模板缺少 KB-CONTRACT:BEGIN/END 标记")
    return text


def block_span(text: str) -> tuple[int, int] | None:
    """Locate the contract block by index — never by regex.

    Returns (start, end), or None when no block exists. Raises KbError when the
    markers are malformed, because a half-written marker is a human problem and
    guessing where the block ends is how you delete someone's SKILL.md: a lone
    BEGIN made apply append a second block, and the next run's `BEGIN.*?END`
    then spanned the body in between and replaced the lot.
    """
    begins = [m.start() for m in re.finditer(re.escape(CONTRACT_BEGIN), text)]
    ends = [m.end() for m in re.finditer(re.escape(CONTRACT_END), text)]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] <= begins[0]:
        raise KbError(
            f"KB-CONTRACT 标记区损坏(BEGIN×{len(begins)},END×{len(ends)}):"
            "请人工修复成恰好一对、且 BEGIN 在 END 之前,再重跑。"
            "为避免误删正文,本次未做任何写入。"
        )
    return begins[0], ends[0]


def contract_status(skill_md: pathlib.Path, template: str) -> str:
    text = read_text_file(skill_md)
    try:
        span = block_span(text)
    except KbError:
        return "broken"
    if span is None:
        return "missing"
    start, end = span
    return "current" if text[start:end].strip() == template.strip() else "stale"


def apply_contract(skill_md: pathlib.Path, template: str) -> None:
    r"""Rewrite only the marker region. Plain slicing — not re.sub, whose
    replacement string parses backslashes: a `grep -E "\d{3}"` in the
    user-editable template blew up with `re.error: bad escape \d`."""
    text = read_text_file(skill_md)
    span = block_span(text)                   # raises on a broken marker
    block = template.strip()
    if span is None:
        new_text = text.rstrip() + "\n\n" + block + "\n"
    else:
        start, end = span
        new_text = text[:start] + block + text[end:]
    skill_md.write_text(new_text, encoding="utf-8")


def contract_targets(skills_dir: pathlib.Path) -> list[pathlib.Path]:
    """SKILL.md files that should carry the contract (whitelist: CONTRACT_SKILLS)."""
    return sorted(
        p / "SKILL.md"
        for p in skills_dir.iterdir()
        if p.is_dir() and skill_base_name(p.name) in CONTRACT_SKILLS and (p / "SKILL.md").is_file()
    )


def cmd_contract(args: argparse.Namespace) -> int:
    skills_dir = pathlib.Path(args.skills_dir).expanduser() if args.skills_dir \
        else _HERE.parent.parent.parent
    if not skills_dir.is_dir():
        raise KbError(f"skills 目录不存在:{skills_dir}")
    template = load_contract_template()
    targets = contract_targets(skills_dir)
    if not targets:
        raise KbError(
            f"{skills_dir} 下没有找到需要注入契约的 skill"
            f"(白名单:{', '.join(sorted(CONTRACT_SKILLS))})"
        )

    pending = applied = 0
    broken: list[pathlib.Path] = []
    for skill_md in targets:
        status = contract_status(skill_md, template)
        if status == "current":
            print(f"[ok     ] {skill_md}")
            continue
        if not args.apply:
            print(f"[{status:<7}] {skill_md}")
            pending += 1
            continue
        try:
            apply_contract(skill_md, template)
        except KbError as exc:            # 标记区损坏:跳过该文件,绝不猜着写
            print(f"[skipped] {skill_md}: {exc}", file=sys.stderr)
            broken.append(skill_md)
            continue
        print(f"[applied] {skill_md}")
        applied += 1

    if args.apply:
        print(f"contract: 实际写入 {applied} 个,已是最新 {len(targets) - applied - len(broken)} 个,"
              f"标记区损坏跳过 {len(broken)} 个(标记区外内容一字未动)。"
              "注意:安装目录副本会被下次 install 覆盖,源码仓也要 apply。")
        return 2 if broken else 0
    print(f"contract: {pending} 个待注入/待刷新(--apply 执行)")
    return 2 if pending else 0


# ---------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    import kb_cases
    import kb_clip
    import kb_docset
    import kb_items
    import kb_read
    import kb_store

    parser = argparse.ArgumentParser(
        prog="kb.py",
        description="GaussDB 客户知识库(导入):规范/工单导入 · 选择列表确认 · 索引(SQLite + sqlite-vec,可选 Neo4j) · 契约注入。"
                    "查询、大盘、引用核对在 gaussdb-kb。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="导入规范(txt/md/docx/doc/pdf/pptx → 条款化;扫描件 PDF 渲染成图片看图转录)、工单(xlsx/csv/md/txt/docx → 一单一文件)"
                                             "或官方文档包(.chm → 官方文档集)")
    p_ingest.add_argument("file", help="文档/工单路径")
    p_ingest.add_argument("--kb", help="KB 目录(默认 $GSDB_KB_DIR 或 <安装根>/kb)")
    p_ingest.add_argument("--kind", choices=("spec", "tickets"),
                          help="材料类型;默认 xlsx/csv 视为工单,其余视为规范")
    p_ingest.add_argument("--slug", help="工单批次名(默认取文件名)")
    p_ingest.add_argument("--redact", action="store_true", help="工单原文脱敏:IP/手机号/证件号/邮箱")
    p_ingest.set_defaults(func=cmd_ingest_any)

    p_index = sub.add_parser("index", help="重建 INDEX.md/RULES.md 与 SQLite 索引(index/store.sqlite);配了 Neo4j 再写图")
    p_index.add_argument("--kb")
    p_index.add_argument("--rebuild", action="store_true", help="清两库重灌(文件是真相)")
    p_index.add_argument("--fill-missing", action="store_true",
                         help="端点试探失败时也逐批重试补没向量的块(端点正常时 index 本来就会补)")
    p_index.add_argument("--budget", type=float, default=kb_docset.DEFAULT_BUDGET_S,
                         help=f"官方文档集建索引的时限(秒,默认 {kb_docset.DEFAULT_BUDGET_S});没建完退出 2,再跑一次接着来;0 = 不限")
    p_index.set_defaults(func=cmd_index_all)
    kb_store.add_admin_subcommands(sub)
    kb_cases.add_subcommands(sub)
    kb_read.add_subcommands(sub)        # 分层读取:导入时核对 L0 / L1、照模板写条目
    kb_items.add_subcommands(sub)       # 类型化条目:add / attach / retype / migrate / profile / taxonomy
    kb_docset.add_subcommands(sub)      # 官方文档集(CHM → 标准知识库):scan / plan / apply / summaries / pack / install
    kb_clip.add_subcommands(sub)        # 网页剪藏(需要登录的内网网页):status / list / ingest / clean

    p_validate = sub.add_parser("validate", help="校验 ID/schema/INDEX 一致性")
    p_validate.add_argument("--kb")
    p_validate.set_defaults(func=cmd_validate)

    p_contract = sub.add_parser("contract", help="向做规范/阈值判断的 skill 注入知识库参考契约")
    p_contract.add_argument("--apply", action="store_true", help="实际写入(默认只扫描)")
    p_contract.add_argument("--skills-dir", help="skills 根目录(默认本 skill 的上级目录)")
    p_contract.set_defaults(func=cmd_contract)
    return parser


def cmd_ingest_any(args: argparse.Namespace) -> int:
    """按 --kind(或后缀)分流:工单 → kb_cases;规范 → 原条款化路径。整个导入持知识库写锁。"""
    import kb_cases
    kb = resolve_kb_dir(args.kb)
    kb.mkdir(parents=True, exist_ok=True)
    from common.kb import chmdoc
    if pathlib.Path(args.file).exists() and chmdoc.is_docset_source(pathlib.Path(args.file)):
        import kb_docset                     # 官方文档包(CHM):走文档集路径,不是条款化
        ensure_kb_skeleton(kb)
        return kb_docset.cmd_scan(argparse.Namespace(file=args.file, slug=args.slug, kb=args.kb))
    kind = args.kind or ("tickets" if pathlib.Path(args.file).suffix.lower() in (".xlsx", ".csv") else "spec")
    with kblock.hold(kb):
        if kind == "tickets":
            ensure_kb_skeleton(kb)
            return kb_cases.cmd_ingest_tickets(args, kb)
        return cmd_ingest(args)


def cmd_index_all(args: argparse.Namespace) -> int:
    """文件级索引(INDEX.md/RULES.md)与 SQLite 索引(<kb>/index/store.sqlite)都做;配了 Neo4j 再写图。整个索引持知识库写锁。"""
    kb = resolve_kb_dir(args.kb)
    if not kb.is_dir():
        raise KbError(f"KB 目录不存在:{kb}(先运行 ingest 或手工创建)")
    with kblock.hold(kb):
        return _index_all_locked(args, kb)


def _index_all_locked(args: argparse.Namespace, kb: pathlib.Path) -> int:
    import kb_docset
    import kb_store

    rc = cmd_index(args)
    if rc != 0:
        return rc
    try:
        rc = kb_store.cmd_index(args)
        budget = getattr(args, "budget", kb_docset.DEFAULT_BUDGET_S)
        rc_docs = kb_docset.index_docsets(kb, budget, rebuild=getattr(args, "rebuild", False))
        if rc_docs:
            from common.kb import layers as kblayers
            kblayers.write_layers(kb, read_version(kb))       # 文档集的索引状态写进 L0
        return max(rc, rc_docs)
    except kb_store.StoreCmdError as exc:
        # 索引写不进去:文件索引已经建好,检索会自动退到文件模式。两句都要说——
        # 只报错会让人以为知识库废了,只报「还能用」会把一个真故障藏起来。
        # 写失败时原索引文件不动(新索引在临时副本上,没换上去):有旧索引就继续用旧的,没有才是文件模式
        print(f"存储        : 写索引失败:{exc}")
        print("            : INDEX.md / RULES.md / CASES.md 已重建,检索照常——有旧索引时继续用旧索引"
              "(内容停在上次 index 成功时),没有索引时走文件模式(词法 + 图文件,无向量召回);"
              "修好后重跑 `kb.py index` 即写索引。")
        return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import kb_cases
    import kb_clip
    import kb_docset
    import kb_items
    import kb_read
    import kb_store
    from common.kb import config as kbconfig
    try:
        return args.func(args)
    except kblock.KbLocked as exc:
        print(f"错误:{exc}", file=sys.stderr)   # 别人正在写:退出码 2(有活没干完),不是 1(坏了)
        return 2
    except (KbError, kb_store.StoreCmdError, kb_cases.CaseCmdError, kb_read.ReadCmdError, kb_items.ItemCmdError,
            kb_clip.ClipCmdError, kbconfig.KbConfigError, spg.StoreError) + kb_docset.ERRORS as exc:
        # StoreError 也要在这里收住:换 embedding 模型那道闸从索引深处抛上来,
        # 漏掉它就会给用户一整屏 traceback —— 看着像崩了,而它其实是在正确地拦着。
        print(f"错误:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
