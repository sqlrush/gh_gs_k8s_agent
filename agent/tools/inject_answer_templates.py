"""把回答模板(common/answer_templates/)注入各技能 SKILL.md 的「## 回答模板」与 AGENTS.md。

2026-10-26 回答模板化:健康检查、故障排查、SQL 优化、知识问答、WDR、榜单各有固定的段落,外加所有回答共用的开头结尾与
「两种读者」写法(需求见 docs/design/2026-10-26-answer-template-requirements.md)。

做法与公共安全红线(tools/inject_red_lines.py)相同:正文只有一份,按技能所属类型拼好后注入,
tests/test_answer_templates_units.py 保证每份副本与正文逐字一致 —— 改了正文忘了注入、或哪份被手工改坏,都在那里变红。
为什么不只写在 AGENTS.md:客户换技能目录时不一定换 AGENTS.md,而模型执行某个技能时最先照着读的是那个技能的 SKILL.md。

用法:
    python3 tools/inject_answer_templates.py            # 注入 / 刷新
    python3 tools/inject_answer_templates.py --check    # 只核对,不一致退出 1(单测与交付前用)
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import Dict, Iterator, List, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANON_DIR = "common/answer_templates"
BEGIN = ("<!-- ANSWER-TEMPLATE:BEGIN — 回答模板,正文在 common/answer_templates/,"
         "由 tools/inject_answer_templates.py 注入,块内修改会被覆盖 -->")
END = "<!-- ANSWER-TEMPLATE:END -->"
SECTION = "## 回答模板"
_BLOCK_RE = re.compile(re.escape(BEGIN) + r"\n(.*?)\n" + re.escape(END), re.S)

# 技能 → 拼哪几段(顺序即拼接顺序)。cite 只给不带知识库契约的两处:带契约的 9 个技能,引用写法在契约里(同一段原文,有守卫)。
SKILL_PARTS: Dict[str, Tuple[str, ...]] = {
    "gaussdb-health": ("common", "health"),
    "gaussdb-config": ("common", "health"),
    "gaussdb-wdr": ("common", "wdr"),
    "gaussdb-lockwait": ("common", "fault", "related"),
    "gaussdb-waitevent": ("common", "fault", "related"),
    "gaussdb-vacuum": ("common", "fault", "related"),
    "gaussdb-memanalyze": ("common", "fault", "related"),
    "gaussdb-sqltune": ("common", "sqltune"),
    "gaussdb-proctune": ("common", "proctune"),
    "gaussdb-explain": ("common", "explain"),
    "gaussdb-topsql": ("common", "ranking"),
    "gaussdb-slowsql": ("common", "ranking"),
    "gaussdb-kb": ("common", "qa", "cite"),
}
# 刻意不套模板的:登录、取 SQL 文本、导入流程是操作而不是分析;存储过程源码查看与耗时排名是取数;
# SQL 审查按客户原有的「SQL 审查输出」格式(2026-10-26 定)。gaussdb-kb-import 只在容器线(gh_agent_k8s)有,
# 是管理员的导入环境,与 gaussdb-kb-init 一样是操作流程。本文件两个仓库同一份。
UNTEMPLATED = ("gaussdb-login", "gaussdb-sqlfetch", "gaussdb-kb-init", "gaussdb-kb-import", "gaussdb-procinfo",
               "gaussdb-topproc", "gaussdb-sqlreview")
AGENTS_PARTS = ("routing", "common", "fault", "related", "qa", "cite")
_AGENTS_ANCHOR = "\n## 输出格式要求"
_SKILL_ANCHOR = "\n## 安全红线"


def part(name: str, root: pathlib.Path = ROOT) -> str:
    return (root / CANON_DIR / ("%s.md" % name)).read_text(encoding="utf-8").strip("\n")


def render(parts: Tuple[str, ...], root: pathlib.Path = ROOT) -> str:
    return "\n\n".join(part(p, root) for p in parts)


def extract_block(text: str) -> str:
    m = _BLOCK_RE.search(text)
    return m.group(1) if m else ""


def _with_block(text: str, body: str, anchor: str) -> str:
    block = "%s\n%s\n%s" % (BEGIN, body, END)
    if BEGIN in text and END in text:
        return _BLOCK_RE.sub(lambda _m: block, text, count=1)
    section = "%s\n\n%s\n" % (SECTION, block)
    if anchor in text:                          # 放在锚点那一节之前,自成一节
        i = text.index(anchor)
        return text[:i].rstrip("\n") + "\n\n" + section + text[i:]
    return text.rstrip("\n") + "\n\n" + section


def targets(root: pathlib.Path = ROOT) -> Iterator[Tuple[pathlib.Path, str, str]]:
    for name, parts in sorted(SKILL_PARTS.items()):
        path = root / "skills" / name / "SKILL.md"
        if path.exists():
            yield path, render(parts, root), _SKILL_ANCHOR
    agents = root / "AGENTS.md"
    if agents.exists():
        yield agents, render(AGENTS_PARTS, root), _AGENTS_ANCHOR


def check(root: pathlib.Path = ROOT) -> List[str]:
    bad = []
    for path, body, _anchor in targets(root):
        text = path.read_text(encoding="utf-8")
        if text.count(BEGIN) != 1 or extract_block(text) != body:
            bad.append(str(path.relative_to(root)))
    return bad


def inject(root: pathlib.Path = ROOT) -> List[str]:
    changed = []
    for path, body, anchor in targets(root):
        text = path.read_text(encoding="utf-8")
        new = _with_block(text, body, anchor)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(str(path.relative_to(root)))
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只核对副本与正文是否一致")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    root = pathlib.Path(args.root)
    if args.check:
        bad = check(root)
        for b in bad:
            print("回答模板副本与 %s 不一致:%s" % (CANON_DIR, b))
        print("核对 %s" % ("通过" if not bad else "不通过:%d 处" % len(bad)))
        return 1 if bad else 0
    changed = inject(root)
    print("注入/刷新 %d 处:%s" % (len(changed), ", ".join(changed) or "无变化"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
