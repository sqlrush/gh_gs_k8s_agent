"""expected:读 `expected.yaml`(预期发现矩阵),核对格式,数出「实际结果」(actual)填了多少。

**为什么需要它:** 这张表是人手填的 —— 跑完一轮,对着每一项跑 skill,把实际报了什么写进 `actual`。
手填 YAML 的坏法很安静:一个以 `*` 开头的值就让整份文件解析不了(第一版就是这样,一直没人发现),
漏写一项的 `actual`、或者 config.py 里加了场景却没在表里登记,表面上都看不出来。
`faultlab.py expected` 在填完之后跑一次:读得通、项项都有 `actual`、编号与排期表一致,再报还剩几项没填。

`actual` 的约定:空串(或 null)= 还没填;填的时候写一段文字(实际报了什么,没报也写明),
不写成列表 / 映射 —— 一项对应一格「实际结果」,与 expect 并排看。
"""
from __future__ import annotations

import pathlib
from typing import Dict, List, Optional, Sequence, Tuple

from . import config as C
from .db import LabError

EXPECTED = C.ROOT / "expected.yaml"
SCENARIO_KEYS = ("code", "title", "signals", "expect", "actual")


def load(path: Optional[pathlib.Path] = None) -> Dict:
    """解析 expected.yaml。读不了 / 不是 YAML / 顶层不对,抛 LabError(带行号,好去改)。"""
    import yaml                                   # 只有这个子命令要它;造故障的路径不依赖 PyYAML
    p = pathlib.Path(path) if path else EXPECTED
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LabError("读不了 %s:%s" % (p, exc))
    except yaml.YAMLError as exc:
        raise LabError("%s 不是合法的 YAML(以 * & ! 等符号开头的值要加引号):%s" % (p.name, exc))
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        raise LabError("%s 顶层要有 scenarios 列表" % p.name)
    return data


def _actual_problem(where: str, item: Dict) -> Optional[str]:
    if "actual" not in item:
        return "%s 缺 actual(待填就写 actual: \"\")" % where
    if item["actual"] is not None and not isinstance(item["actual"], str):
        return "%s 的 actual 要写成一段文字,不是 %s" % (where, type(item["actual"]).__name__)
    return None


def problems(data: Dict, codes: Sequence[str] = ()) -> List[str]:
    """格式问题清单(空 = 没问题)。codes 给了就核对与排期表的编号一一对应。"""
    out: List[str] = []
    seen: List[str] = []
    for i, item in enumerate(data.get("scenarios") or []):
        if not isinstance(item, dict):
            out.append("scenarios[%d] 不是键值映射" % i)
            continue
        code = str(item.get("code") or "")
        where = "场景 %s" % (code or "#%d" % i)
        out += ["%s 缺 %s" % (where, k) for k in SCENARIO_KEYS if k != "actual" and not item.get(k)]
        out += [p for p in (_actual_problem(where, item),) if p]
        if code in seen:
            out.append("%s 重复登记" % where)
        seen.append(code)
    for i, item in enumerate(data.get("cross_cutting") or []):
        where = "跨场景项 #%d" % (i + 1)
        if not isinstance(item, dict):
            out.append("%s 不是键值映射" % where)
            continue
        out += [p for p in (_actual_problem(where, item),) if p]
    if codes:
        missing = [c for c in codes if c not in seen]
        extra = [c for c in seen if c and c not in codes]
        if missing:
            out.append("排期表里有、矩阵里没登记的场景:%s" % ", ".join(missing))
        if extra:
            out.append("矩阵里有、排期表里没有的场景:%s" % ", ".join(extra))
    return out


def progress(data: Dict) -> Tuple[int, List[str]]:
    """(已填项数, 待填项的编号)。场景按 code,跨场景项按「项」的名字。"""
    filled, pending = 0, []
    items = [(str(s.get("code") or "?"), s) for s in data.get("scenarios") or [] if isinstance(s, dict)]
    items += [("跨场景:%s" % (c.get("项") or "?"), c) for c in data.get("cross_cutting") or [] if isinstance(c, dict)]
    for name, item in items:
        if str(item.get("actual") or "").strip():
            filled += 1
        else:
            pending.append(name)
    return filled, pending


def render(path: Optional[pathlib.Path] = None) -> Tuple[str, bool]:
    """(给人看的一段文字, 格式是否没问题)。"""
    data = load(path)
    issues = problems(data, [s.code for s in C.ALL_SCENARIOS])
    filled, pending = progress(data)
    lines = ["# 预期发现矩阵(expected.yaml)",
             "- 场景 %d 项 · 跨场景 %d 项 · actual 已填 %d、待填 %d" % (
                 len(data.get("scenarios") or []), len(data.get("cross_cutting") or []), filled, len(pending))]
    if pending:
        lines.append("- 待填:%s" % ", ".join(pending))
    if issues:
        lines += ["", "## 格式问题(先改掉再填)"] + ["- %s" % m for m in issues]
    return "\n".join(lines), not issues
