"""把公共安全红线(common/red_lines.md)注入每个 SKILL.md 的「## 安全红线」与 AGENTS.md。

为什么是「每份一个副本」而不是「AGENTS.md 一份」:2026-09-02 把 14 份公共红线抽到 AGENTS.md 去重,而安装脚本不拷 AGENTS.md,
客户只换了 skill 目录,安全审查 diff SKILL.md 看到红线整段消失(09-10 反馈)。银行客户要的是每个交付文件里看得见。
所以正文只有一份,副本由本工具注入、由 tests/test_red_lines_units.py 保证逐字一致。

用法:
    python3 tools/inject_red_lines.py            # 注入 / 刷新 19 个 SKILL.md 与 AGENTS.md
    python3 tools/inject_red_lines.py --check    # 只核对,不一致退出 1(单测与交付前用)
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import List

ROOT = pathlib.Path(__file__).resolve().parents[1]
CANON = "common/red_lines.md"
BEGIN = "<!-- RED-LINES:BEGIN — 公共安全红线,正文在 common/red_lines.md,由 tools/inject_red_lines.py 注入,块内修改会被覆盖 -->"
END = "<!-- RED-LINES:END -->"
AGENTS_SCRIPT = "<skill>.py"          # AGENTS.md 不属于某一个 skill,最后一条写成泛指
_SECTION = "## 安全红线"
_KB_BLOCK = "<!-- KB-CONTRACT:BEGIN"
_BLOCK_RE = re.compile(re.escape(BEGIN) + r"\n(.*?)\n" + re.escape(END), re.S)


def canonical_text(root: pathlib.Path = ROOT) -> str:
    return (root / CANON).read_text(encoding="utf-8").strip("\n")


# 目录名推不出主脚本名的 skill 在这里点名。gaussdb-kb / gaussdb-kb-import 的入口都叫 kb.py:
# 前者是查询,后者是导入,两个镜像各装一个,SKILL.md 里 `{script}` 渲染出来必须都是 kb.py。
_MAIN_SCRIPT_OVERRIDES = {"gaussdb-kb-import": "kb.py", "gaussdb-kb-init": "kb_init.py"}


def main_script(skill_dir: pathlib.Path) -> str:
    """gaussdb-sqlfetch → sqlfetch.py。每个 skill 的主脚本就叫这个名字(tests 里核过存在)。"""
    return _MAIN_SCRIPT_OVERRIDES.get(skill_dir.name) or skill_dir.name.split("-", 1)[-1] + ".py"


def render(script: str, root: pathlib.Path = ROOT) -> str:
    return canonical_text(root).replace("{script}", script)


def extract_block(text: str) -> str:
    m = _BLOCK_RE.search(text)
    return m.group(1) if m else ""


def _with_block(text: str, body: str, anchor_after_heading: bool) -> str:
    block = "%s\n%s\n%s" % (BEGIN, body, END)
    if BEGIN in text and END in text:
        return _BLOCK_RE.sub(lambda _m: block, text, count=1)
    if anchor_after_heading and ("\n" + _SECTION + "\n") in text:
        # 已有「## 安全红线」:块放在标题之后,skill 特有的条目留在块后面
        head, tail = text.split("\n" + _SECTION + "\n", 1)
        return head + "\n" + _SECTION + "\n\n" + block + "\n" + tail.lstrip("\n") if tail.strip() else \
            head + "\n" + _SECTION + "\n\n" + block + "\n"
    section = "\n%s\n\n%s\n" % (_SECTION, block)
    if _KB_BLOCK in text:                       # 知识库契约块在文件尾,红线放它前面
        i = text.index(_KB_BLOCK)
        return text[:i].rstrip("\n") + "\n" + section + "\n" + text[i:]
    return text.rstrip("\n") + "\n" + section


def _targets(root: pathlib.Path):
    for path in sorted((root / "skills").glob("gaussdb-*/SKILL.md")):
        yield path, render(main_script(path.parent), root), True
    agents = root / "AGENTS.md"
    if agents.exists():
        yield agents, render(AGENTS_SCRIPT, root), False


def _agents_with_block(text: str, body: str) -> str:
    """AGENTS.md:块放在「## 数据库连接规则」之前,自成「## 安全红线」一节。"""
    if BEGIN in text and END in text:
        return _BLOCK_RE.sub(lambda _m: "%s\n%s\n%s" % (BEGIN, body, END), text, count=1)
    section = "%s\n\n%s\n%s\n%s\n" % (_SECTION, BEGIN, body, END)
    marker = "\n## 数据库连接规则"
    if marker in text:
        i = text.index(marker)
        return text[:i].rstrip("\n") + "\n\n" + section + text[i:]
    return text.rstrip("\n") + "\n\n" + section


def check(root: pathlib.Path = ROOT) -> List[str]:
    bad = []
    for path, body, is_skill in _targets(root):
        text = path.read_text(encoding="utf-8")
        if extract_block(text) != body or text.count(BEGIN) != 1:
            bad.append(str(path.relative_to(root)))
    return bad


def inject(root: pathlib.Path = ROOT) -> List[str]:
    changed = []
    for path, body, is_skill in _targets(root):
        text = path.read_text(encoding="utf-8")
        new = _with_block(text, body, True) if is_skill else _agents_with_block(text, body)
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
            print("红线副本与 common/red_lines.md 不一致:%s" % b)
        print("核对 %s" % ("通过" if not bad else "不通过:%d 处" % len(bad)))
        return 1 if bad else 0
    changed = inject(root)
    print("注入/刷新 %d 处:%s" % (len(changed), ", ".join(changed) or "无变化"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
