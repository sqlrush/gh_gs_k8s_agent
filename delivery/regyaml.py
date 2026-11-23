"""白名单脚本定义(scripts/registry/<技能>/<脚本>.yaml)的只读解析器,只用标准库。

为什么不用 PyYAML:转化脚本在交付执行人的机器上只要求 git + Python 3.9+。这些文件的格式是固定的——
顶层 name / description / readonly / sql(一律 `sql: |` 块)/ params(列表,每项 key / type / description),
这里只认这个格式,认不出就报错(不猜)。守卫测试核对:对仓库里每个文件,结果与 PyYAML 完全一致。
"""
from __future__ import annotations

import pathlib
import re
from typing import Any, Dict, List

_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s(.*))?$")


class FormatError(ValueError):
    pass


def _scalar(raw: str, where: str) -> Any:
    s = raw.strip()
    if not s:
        return None
    if s.startswith("'"):
        if not s.endswith("'") or len(s) < 2:
            raise FormatError("%s:单引号没闭合" % where)
        return s[1:-1].replace("''", "'")
    if s.startswith('"'):
        if not s.endswith('"') or len(s) < 2:
            raise FormatError("%s:双引号没闭合" % where)
        body = s[1:-1]
        if "\\" in body.replace('\\"', "").replace("\\\\", ""):
            raise FormatError("%s:双引号里有不认识的转义" % where)
        return body.replace('\\"', '"').replace("\\\\", "\\")
    if s[0] in "|>[{&*!%@`":
        raise FormatError("%s:不支持的写法 %r" % (where, s[:20]))
    if " #" in s:
        s = s.split(" #", 1)[0].rstrip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _block(lines: List[str], i: int, where: str):
    """`key: |` 之后的块:去掉公共缩进,保留一个结尾换行(YAML 的 clip)。返回 (文本, 下一行号)。"""
    body, indent = [], None
    while i < len(lines):
        line = lines[i]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if line.strip():
            n = len(line) - len(line.lstrip(" "))
            indent = n if indent is None else min(indent, n)
        body.append(line)
        i += 1
    if indent is None:
        raise FormatError("%s:sql 块是空的" % where)
    text = "\n".join(l[indent:] if l.strip() else "" for l in body).rstrip("\n")
    return text + "\n", i


def _params(lines: List[str], i: int, where: str):
    items: List[Dict[str, Any]] = []
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if not line.startswith((" ", "\t")):
            break
        s = line.strip()
        if s.startswith("- "):
            items.append({})
            s = s[2:]
        if not items:
            raise FormatError("%s:params 第一项没有以「- 」开头" % where)
        m = _KEY.match(s)
        if not m:
            raise FormatError("%s:认不出的参数行 %r" % (where, s[:40]))
        items[-1][m.group(1)] = _scalar(m.group(2) or "", where)
        i += 1
    return items, i


def parse(text: str, where: str = "<yaml>") -> Dict[str, Any]:
    lines = text.splitlines()
    out: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = _KEY.match(line)
        if not m or line.startswith((" ", "\t")):
            raise FormatError("%s 第 %d 行:认不出的顶层行 %r" % (where, i + 1, line[:40]))
        key, rest = m.group(1), (m.group(2) or "").strip()
        if rest == "|":
            out[key], i = _block(lines, i + 1, where)
        elif key == "params" and not rest:
            out[key], i = _params(lines, i + 1, where)
        else:
            out[key] = _scalar(rest, where)
            i += 1
    for need in ("name", "sql"):
        if need not in out:
            raise FormatError("%s:缺 %s" % (where, need))
    return out


def load(path: pathlib.Path) -> Dict[str, Any]:
    return parse(path.read_text(encoding="utf-8"), str(path))
