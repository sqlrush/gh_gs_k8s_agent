#!/usr/bin/env python3
"""验收题库的离线校验:题目与评分标准本身写得对不对(不连库、不跑模型)。

    python3 acceptance/check_bank.py                    # 校验 acceptance/bank/ 下的全部版本
    python3 acceptance/check_bank.py --bank v1 --list   # 列出题目:编号、方面、冒烟、阶段、要的故障与知识

查的是「评分标准能不能被可靠地执行」:每题分值合计 100;点名的技能真实存在、在那个环境里装着;引用的知识库编号在验收知识库里;
真值指向的故障、夹具、环境事实都有定义;占位符都认识;版式开关与 agent/tools/answer_check.py 的回答模板一致;
红线题有一票否决项。题库写法见 acceptance/README.md,方案见 docs/智能体验收方案.md。有 ✗ 退出 1。
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
BANK_DIR = ROOT / "acceptance" / "bank"
FIXTURE_TRUTH = ROOT / "acceptance" / "fixtures" / "truth.yaml"
SKILLS_DIR = ROOT / "agent" / "skills"
SAMPLE_KB = SKILLS_DIR / "gaussdb-kb-import" / "testdata" / "sample-kb"
ANSWER_CHECK = ROOT / "agent" / "tools" / "answer_check.py"

ASPECTS = {"A": "健康检查", "B": "故障排查", "C": "SQL 优化", "D": "榜单", "E": "WDR", "F": "知识问答",
           "G": "知识引用", "H": "安全红线", "I": "不乱答", "J": "交互与路由", "K": "知识库导入"}
# faultlab 的场景编号(技能仓库 tools/faultlab/expected.yaml 的 scenarios;那边增删场景时这里同步)
FAULTLAB = frozenset(["S%d" % i for i in range(1, 8)] + ["K%d" % i for i in range(1, 11)] + ["D"]
                     + ["V%d" % i for i in range(1, 8)] + ["L%d" % i for i in range(1, 6)])
KB_IMPORT_ONLY = frozenset({"gaussdb-kb-import", "gaussdb-kb-init"})     # 诊断环境的镜像里删掉了这两个
KB_IMPORT_SKILLS = frozenset({"gaussdb-kb", "gaussdb-kb-import", "gaussdb-kb-init"})
ENVS, PHASES, MODES = ("runtime", "kb-import"), ("live", "after"), ("paste", "upload", "db", "clip")
EXTRA_TEMPLATES = ("review", "kbimport", "any")    # answer_check 不管的三种:SQL 审查、导入确认清单、不限版式
SIMPLE_PLACEHOLDERS = frozenset({"instance", "alias", "broken", "start", "end", "old",
                                 "snap_begin", "snap_end", "clip_batch"})
LAB_KEYS = frozenset({"root_pid", "depth", "object", "sql_id"})
ENV_KEYS = frozenset({"window", "conn", "snaps", "retention", "instance.version"})
RULE_KEYS = frozenset({"no_secret_pattern"})
ITEM_LISTS = ("points", "handling", "cite", "forbid")
# 各层允许的键:YAML 流式写法里文字没加引号又带了半角逗号,会被悄悄拆成一个多余的空键 —— 靠这几张表抓出来
QUESTION_KEYS = frozenset({"id", "title", "smoke", "env", "phase", "needs", "attach", "ask", "followups", "rubric"})
RUBRIC_KEYS = frozenset({"format", "tools", "notes"} | set(ITEM_LISTS))
FORMAT_KEYS = frozenset({"w", "template", "status_band", "both_views", "followup", "extra"})
TOOLS_KEYS = frozenset({"w", "must", "should", "must_not"})
TOOL_ENTRY_KEYS = frozenset({"skill", "any_of", "cmd", "tool", "args", "why"})
ITEM_KEYS = frozenset({"w", "veto", "text", "truth", "by"})
ATTACH_KEYS = frozenset({"file", "mode", "strip_comments", "generated"})
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_ID = re.compile(r"^([A-K])(\d+)$")
_DOCSET_ID = re.compile(r"^PD-G25132C-\d{5}$")
_KB_REF = re.compile(r"(?<![A-Za-z0-9-])(GS-[A-Z]+-\d{3}|RB-[A-Z]+-\d{3}|KD-\d{4}|WK-\d{4}|PD-G25132C-\d{5}|PD-\d{4}"
                     r"|EP-[A-Z]+|BL-[A-Z0-9_-]+|S\d-\d{8}-[A-Z]+-[^\s,，;；:：、。()（）\[\]\"']+)")


@dataclass(frozen=True)
class Problem:
    where: str
    msg: str


def load_answer_check():
    spec = importlib.util.spec_from_file_location("acceptance_answer_check", ANSWER_CHECK)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                     # 它里面有 dataclass,执行前要先登记模块
    spec.loader.exec_module(mod)
    return mod


def sample_kb_ids(root: pathlib.Path = SAMPLE_KB) -> Set[str]:
    """验收知识库里样例知识的全部编号:md 条目的 front matter 与 rules/*.yaml。"""
    ids: Set[str] = set()
    for md in root.rglob("*.md"):
        m = re.search(r"^id:\s*(\S+)\s*$", md.read_text(encoding="utf-8"), re.M)
        if m:
            ids.add(m.group(1))
    for f in sorted((root / "rules").glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        rows = data if isinstance(data, list) else (data.get("rules") or data.get("items") or [])
        ids.update(r["id"] for r in rows if isinstance(r, dict) and r.get("id"))
    return ids


def skills(root: pathlib.Path = SKILLS_DIR) -> Set[str]:
    return {p.parent.name for p in root.glob("*/SKILL.md")}


@dataclass(frozen=True)
class Context:
    skills: frozenset
    kb_ids: frozenset
    fixtures: Dict[str, dict]
    templates: frozenset
    status_band: frozenset
    both_views: frozenset


def build_context() -> Context:
    ac = load_answer_check()
    fixtures = yaml.safe_load(FIXTURE_TRUTH.read_text(encoding="utf-8")) or {}
    return Context(frozenset(skills()), frozenset(sample_kb_ids()), fixtures,
                   frozenset(list(ac.SECTIONS) + list(EXTRA_TEMPLATES)),
                   frozenset(ac.STATUS_BAND), frozenset(ac.BOTH_VIEWS))


def kb_id_ok(ctx: Context, kid: str) -> bool:
    return kid in ctx.kb_ids or bool(_DOCSET_ID.match(kid))


def check_placeholders(where: str, text: str, phase: str) -> List[Problem]:
    out: List[Problem] = []
    for name in _PLACEHOLDER.findall(text):
        if name in SIMPLE_PLACEHOLDERS:
            continue
        kind, _, arg = name.partition(":")
        if kind == "sql_id" and arg in FAULTLAB:
            continue
        if kind == "pid" and arg.split("/")[0] in FAULTLAB:
            if phase != "live":
                out.append(Problem(where, "{%s} 要会话还活着,只能用在 phase: live 的题里" % name))
            continue
        out.append(Problem(where, "不认识的占位符 {%s}" % name))
    return out


def _split_refs(body: str) -> List[str]:
    return [p.strip() for p in body.split(",") if p.strip()]


def check_truth(where: str, truth: str, q: dict, ctx: Context) -> List[Problem]:
    """truth 的写法:<来源>:<内容>,来源见 README「真值」一节;逗号分隔多个。"""
    prefix, _, body = truth.partition(":")
    needs = q.get("needs") or {}
    out: List[Problem] = []
    if prefix == "lab":
        for ref in _split_refs(body):
            code, _, key = ref.partition(".")
            if code not in FAULTLAB:
                out.append(Problem(where, "真值 %s:没有这个 faultlab 场景 %s" % (truth, code)))
            elif code not in (needs.get("faultlab") or []):
                out.append(Problem(where, "真值用到场景 %s,needs.faultlab 里要列上" % code))
            if key and key not in LAB_KEYS:
                out.append(Problem(where, "真值 %s:场景的键只能是 %s" % (truth, "、".join(sorted(LAB_KEYS)))))
    elif prefix == "kb":
        for ref in _split_refs(body):
            if ref in ("none", "unchanged"):
                continue
            kid = ref.split(".", 1)[0]
            if not kb_id_ok(ctx, kid):
                out.append(Problem(where, "真值 %s:验收知识库里没有 %s" % (truth, kid)))
            elif kid not in (needs.get("kb") or []):
                out.append(Problem(where, "真值用到 %s,needs.kb 里要列上" % kid))
    elif prefix == "env":
        if body not in ENV_KEYS:
            out.append(Problem(where, "真值 %s:环境事实只能是 %s" % (truth, "、".join(sorted(ENV_KEYS)))))
    elif prefix == "tool":
        if body not in ctx.skills:
            out.append(Problem(where, "真值 %s:没有这个技能" % truth))
    elif prefix == "fixture":
        if body not in ctx.fixtures:
            out.append(Problem(where, "真值 %s:acceptance/fixtures/truth.yaml 里没有这个键" % truth))
        if not q.get("attach"):
            out.append(Problem(where, "真值 %s 来自夹具,题目要有 attach" % truth))
    elif prefix == "rule":
        if body not in RULE_KEYS:
            out.append(Problem(where, "真值 %s:内置规则只能是 %s" % (truth, "、".join(sorted(RULE_KEYS)))))
    elif prefix == "turn":
        if not re.match(r"^\d+\.\w+$", body) or not q.get("followups"):
            out.append(Problem(where, "真值 %s:要写成 turn:<轮次>.<键>,且题目要有 followups" % truth))
    else:
        out.append(Problem(where, "真值 %s:来源只能是 lab / kb / env / tool / fixture / rule / turn" % truth))
    return out


def _tool_names(entry) -> List[str]:
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        if "skill" in entry:
            return [entry["skill"]]
        return list(entry.get("any_of") or [])
    return []


def check_tools(where: str, tools: dict, env: str, ctx: Context) -> Tuple[int, List[Problem]]:
    out: List[Problem] = []
    avail = KB_IMPORT_SKILLS if env == "kb-import" else ctx.skills - KB_IMPORT_ONLY
    for group in ("must", "should", "must_not"):
        for entry in tools.get(group) or []:
            if isinstance(entry, dict) and len({"skill", "any_of", "cmd", "tool"} & set(entry)) != 1:
                out.append(Problem(where, "tools.%s 每项只能写 skill / any_of / cmd / tool 之一:%r" % (group, entry)))
                continue
            names = _tool_names(entry)
            for name in names:
                if name not in ctx.skills:
                    out.append(Problem(where, "tools.%s:没有这个技能 %s" % (group, name)))
            if group == "must" and names and not any(n in avail for n in names):
                out.append(Problem(where, "tools.must:%s 不在 %s 环境里" % ("/".join(names), env)))
    return _weight(tools.get("w")), out


def _weight(w) -> int:
    return w if isinstance(w, int) and not isinstance(w, bool) and w > 0 else -10_000


def check_items(where: str, rubric: dict, q: dict, ctx: Context) -> Tuple[int, int, List[Problem]]:
    """各条检查点:分值、真值、写法;返回 (分值合计, 否决项个数, 问题)。"""
    total, vetoes, out = 0, 0, []
    if not rubric.get("points"):
        out.append(Problem(where, "rubric.points 不能为空"))
    for name in ITEM_LISTS:
        for i, item in enumerate(rubric.get(name) or []):
            at = "%s rubric.%s[%d]" % (where, name, i)
            if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                out.append(Problem(at, "每条要有 text"))
                continue
            if item.get("veto"):
                if name != "forbid" or "w" in item:
                    out.append(Problem(at, "veto 只用在 forbid 里,且不带分值"))
                vetoes += 1
            elif _weight(item.get("w")) < 0:
                out.append(Problem(at, "w 要是正整数"))
            else:
                total += item["w"]
            if item.get("by") not in (None, "rule", "judge"):
                out.append(Problem(at, "by 只能是 rule / judge"))
            if item.get("truth"):
                out.extend(check_truth(at, str(item["truth"]), q, ctx))
    return total, vetoes, out


def check_format(where: str, fmt: dict, ctx: Context) -> Tuple[int, List[Problem]]:
    out: List[Problem] = []
    kind = fmt.get("template")
    if kind not in ctx.templates:
        out.append(Problem(where, "format.template 只能是 %s" % " / ".join(sorted(ctx.templates))))
    if fmt.get("status_band") and kind not in ctx.status_band:
        out.append(Problem(where, "format.status_band:%s 版式没有总体状态带(见 answer_check.STATUS_BAND)" % kind))
    if fmt.get("both_views") and kind not in ctx.both_views:
        out.append(Problem(where, "format.both_views:%s 版式不分两种读者(见 answer_check.BOTH_VIEWS)" % kind))
    return _weight(fmt.get("w")), out


def _texts(q: dict) -> Iterable[str]:
    yield str(q.get("ask") or "")
    yield from (str(f) for f in q.get("followups") or [])
    rubric = q.get("rubric") or {}
    yield from (str(e) for e in (rubric.get("format") or {}).get("extra") or [])
    for name in ITEM_LISTS:
        yield from (str(i.get("text") or "") for i in rubric.get(name) or [] if isinstance(i, dict))


def check_keys(where: str, q: dict) -> List[Problem]:
    rubric = q.get("rubric") or {}
    tools = rubric.get("tools") or {}
    layers = [("题目", q, QUESTION_KEYS), ("rubric", rubric, RUBRIC_KEYS), ("format", rubric.get("format") or {}, FORMAT_KEYS),
              ("tools", tools, TOOLS_KEYS), ("attach", q.get("attach") or {}, ATTACH_KEYS)]
    layers += [("tools.%s" % g, e, TOOL_ENTRY_KEYS) for g in ("must", "should", "must_not")
               for e in tools.get(g) or [] if isinstance(e, dict)]
    layers += [("rubric.%s" % n, i, ITEM_KEYS) for n in ITEM_LISTS for i in rubric.get(n) or [] if isinstance(i, dict)]
    out: List[Problem] = []
    for name, obj, allowed in layers:
        extra = sorted(str(k) for k in obj if k not in allowed)
        if extra:
            out.append(Problem(where, "%s 里有不认识的键 %s(文字里有半角逗号时要加引号)" % (name, "、".join(extra))))
    return out


def check_question(q: dict, aspect: str, ctx: Context) -> List[Problem]:
    qid = str(q.get("id"))
    where = qid
    out: List[Problem] = check_keys(where, q)
    m = _ID.match(qid)
    if not m or m.group(1) != aspect:
        out.append(Problem(where, "编号要是 %s<数字>" % aspect))
    for key in ("title", "ask", "rubric", "needs"):
        if not q.get(key):
            out.append(Problem(where, "缺 %s" % key))
    if not isinstance(q.get("smoke"), bool):
        out.append(Problem(where, "smoke 要写 true / false"))
    env, phase = q.get("env"), q.get("phase")
    if env not in ENVS or phase not in PHASES:
        out.append(Problem(where, "env 只能是 %s,phase 只能是 %s" % (" / ".join(ENVS), " / ".join(PHASES))))
    if env == "kb-import" and phase == "live":
        out.append(Problem(where, "导入环境的题不依赖故障现场,phase 写 after"))
    out.extend(check_needs(where, q.get("needs") or {}, ctx))
    out.extend(check_attach(where, q.get("attach")))
    for text in _texts(q):
        out.extend(check_placeholders(where, text, phase))
        out.extend(Problem(where, "提到的 %s 不在验收知识库里" % k) for k in _KB_REF.findall(text) if not kb_id_ok(ctx, k))
    rubric = q.get("rubric") or {}
    fw, p1 = check_format(where, rubric.get("format") or {}, ctx)
    tw, p2 = check_tools(where, rubric.get("tools") or {}, env, ctx)
    iw, vetoes, p3 = check_items(where, rubric, q, ctx)
    out.extend(p1 + p2 + p3)
    if fw < 0 or tw < 0:
        out.append(Problem(where, "format.w 与 tools.w 要是正整数"))
    elif fw + tw + iw != 100:
        out.append(Problem(where, "分值合计 %d,应为 100(format %d + tools %d + 各条 %d)" % (fw + tw + iw, fw, tw, iw)))
    if aspect == "H" and vetoes == 0:
        out.append(Problem(where, "红线题至少要有一条 veto"))
    return out


def check_needs(where: str, needs: dict, ctx: Context) -> List[Problem]:
    out = [Problem(where, "needs.faultlab:没有这个场景 %s" % c) for c in needs.get("faultlab") or [] if c not in FAULTLAB]
    out += [Problem(where, "needs.kb:验收知识库里没有 %s" % k) for k in needs.get("kb") or [] if not kb_id_ok(ctx, k)]
    return out


def check_attach(where: str, attach: Optional[dict]) -> List[Problem]:
    if not attach:
        return []
    out: List[Problem] = []
    f = str(attach.get("file") or "")
    if attach.get("mode") not in MODES:
        out.append(Problem(where, "attach.mode 只能是 %s" % " / ".join(MODES)))
    if not f.startswith("faultlab:") and not (ROOT / f).is_file():
        out.append(Problem(where, "attach.file 不存在:%s" % f))
    return out


def check_fixture_truth(ctx: Context) -> List[Problem]:
    out: List[Problem] = []
    for key, spec in ctx.fixtures.items():
        if not isinstance(spec, dict) or not spec.get("text") or not (ROOT / str(spec.get("file") or "")).is_file():
            out.append(Problem("truth.yaml %s" % key, "每个键要有 text,file 要存在"))
    return out


def load_bank(version_dir: pathlib.Path) -> Tuple[List[Tuple[pathlib.Path, dict]], List[Problem]]:
    docs, bad = [], []
    for f in sorted(version_dir.glob("*.yaml")):
        try:
            docs.append((f, yaml.safe_load(f.read_text(encoding="utf-8")) or {}))
        except yaml.YAMLError as e:
            bad.append(Problem(f.name, "YAML 解析失败:%s" % str(e).replace("\n", " ")))
    return docs, bad


def check_bank(version_dir: pathlib.Path, ctx: Context) -> Tuple[List[dict], List[Problem]]:
    docs, out = load_bank(version_dir)
    questions: List[dict] = []
    seen: Dict[str, str] = {}
    for f, doc in docs:
        aspect = str(doc.get("aspect") or "")
        if doc.get("bank") != version_dir.name or aspect not in ASPECTS or not f.name.startswith(aspect + "-"):
            out.append(Problem(f.name, "文件头要有 bank: %s 与 aspect(A–K),文件名以「方面-」开头" % version_dir.name))
            continue
        for q in doc.get("questions") or []:
            qid = str(q.get("id"))
            if qid in seen:
                out.append(Problem(qid, "编号重复(%s 与 %s)" % (seen[qid], f.name)))
            seen[qid] = f.name
            out.extend(Problem("%s %s" % (f.name, p.where), p.msg) for p in check_question(q, aspect, ctx))
            questions.append(dict(q, aspect=aspect))
    if not questions:
        out.append(Problem(version_dir.name, "题库里没有题"))
    return questions, out


def summary(questions: Sequence[dict]) -> str:
    by = {a: sum(1 for q in questions if q["aspect"] == a) for a in ASPECTS}
    parts = ["%s %s %d" % (a, ASPECTS[a], n) for a, n in by.items() if n]
    smoke = sum(1 for q in questions if q.get("smoke"))
    live = sum(1 for q in questions if q.get("phase") == "live")
    return "%d 题(冒烟 %d、故障现场题 %d):%s" % (len(questions), smoke, live, " · ".join(parts))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="验收题库的离线校验")
    ap.add_argument("--bank", default="", help="只校验这个版本(如 v1),默认全部")
    ap.add_argument("--list", action="store_true", help="列出题目")
    args = ap.parse_args(argv)
    ctx = build_context()
    problems = check_fixture_truth(ctx)
    dirs = [BANK_DIR / args.bank] if args.bank else sorted(p for p in BANK_DIR.iterdir() if p.is_dir())
    for d in dirs:
        questions, found = check_bank(d, ctx)
        problems.extend(found)
        print("%s %s" % (d.name, summary(questions)))
        if args.list:
            for q in questions:
                needs = q.get("needs") or {}
                print("  %-4s %-4s %-5s %-9s %-36s 故障 %-14s 知识 %s" % (
                    q.get("id"), "冒烟" if q.get("smoke") else "", q.get("phase"), q.get("env"), q.get("title"),
                    ",".join(needs.get("faultlab") or []) or "-", ",".join(needs.get("kb") or []) or "-"))
    for p in problems:
        print("✗ %s:%s" % (p.where, p.msg))
    print("✓ 题库校验通过" if not problems else "✗ %d 处问题" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
