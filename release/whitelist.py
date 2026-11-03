"""白名单核对:本版本应有的每条脚本是否都已在中间件登记、内容是否一致。只读,不执行任何白名单 SQL。

**为什么要按内容核对。** 运行时只按脚本名找脚本(agent/common/grmp/client.py:拉列表建 {cmd_name: id} 再按 id 调用),
不比正文 —— 正文不一致时静默执行旧 SQL;参数定义不一致时中间件拒绝调用(2026-10-24 health.stale_stats 加 min_pages 的教训)。

- 应有:交付给白名单团队的 docs/delivery/scripts.sql(全量 DML)。正文 = 字面量里 '' 还原成 ' 之后的原样(含末尾换行),
  与库里 script_content 逐字节相同,md5 口径与增量文件末尾的自检 SQL、gh_skill tools/delivery_delta.py 一致。
- 已登记:grmp.script_config 里 scene='AGENT' 的行(只读账号直连,或白名单团队按 EXPORT_SQL 导出的 CSV)。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

SCENE = "AGENT"
EXPORT_SQL = ("SELECT script_name, md5(script_content) AS content_md5, parameter_config, is_valid\n"
              "  FROM grmp.script_config WHERE scene = 'AGENT' ORDER BY script_name;")

OK, MISSING, CONTENT, PARAMS, DUPLICATE, INVALID = "ok", "missing", "content", "params", "duplicate", "invalid"
LABELS = {OK: "已登记且一致", MISSING: "未登记", CONTENT: "正文不一致", PARAMS: "参数不一致", DUPLICATE: "重名",
          INVALID: "未生效"}


class WhitelistError(ValueError):
    pass


@dataclass(frozen=True)
class Script:
    name: str
    md5: str
    params: Tuple[Tuple[str, str], ...]          # (key, type),顺序有意义;value / autoAcquire 不算


@dataclass(frozen=True)
class Registered:
    name: str
    md5: str
    params: Tuple[Tuple[str, str], ...]
    valid: bool


@dataclass(frozen=True)
class Finding:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    expected: int
    findings: List[Finding] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)       # 中间件里有、本版本不需要的(只列出,不算失败)

    @property
    def problems(self) -> List[Finding]:
        return [f for f in self.findings if f.status != OK]

    @property
    def passed(self) -> bool:
        return not self.problems

    def counts(self) -> Dict[str, int]:
        out = {k: 0 for k in LABELS}
        for f in self.findings:
            out[f.status] += 1
        return out


def param_keys(text: Optional[str]) -> Tuple[Tuple[str, str], ...]:
    try:
        items = json.loads(text or "[]")
        return tuple((str(p.get("key")), str(p.get("type"))) for p in items)
    except (ValueError, AttributeError, TypeError):
        return (("?", str(text)),)


# -- 解析交付 DML --------------------------------------------------------------------------------

_INSERT = re.compile(r"INSERT\s+INTO\s+grmp\.script_config\s*\(([^)]*)\)\s*VALUES\s*\(", re.I)


def _values(text: str, start: int) -> Tuple[List[Optional[str]], int]:
    """从 VALUES ( 之后解析到配对的 ),返回各值:字符串字面量还原 '',其余(函数、数字、NULL)按原文,NULL → None。"""
    vals: List[Optional[str]] = []
    i, n = start, len(text)
    cur: List[str] = []
    is_str, depth = False, 0
    while i < n:
        c = text[i]
        if c == "'":
            if "".join(cur).strip():
                raise WhitelistError("字符串字面量前有前缀(偏移 %d),交付文件不该有这种写法" % i)
            cur = []                    # 逗号与引号之间的空白不属于值
            j = i + 1
            buf = []
            while True:
                k = text.find("'", j)
                if k < 0:
                    raise WhitelistError("字符串字面量没有闭合(偏移 %d)" % i)
                buf.append(text[j:k])
                if k + 1 < n and text[k + 1] == "'":
                    buf.append("'")
                    j = k + 2
                    continue
                i = k + 1
                break
            cur.append("".join(buf))
            is_str = True
            continue
        if c == "(":
            depth += 1
        elif c == ")" and depth > 0:
            depth -= 1
        elif c in ",)" and depth == 0:
            raw = "".join(cur) if is_str else "".join(cur).strip()
            vals.append(None if not is_str and raw.upper() == "NULL" else raw)
            cur, is_str = [], False
            if c == ")":
                return vals, i + 1
            i += 1
            continue
        if not is_str:                  # 字面量之后到逗号之间只会有空白
            cur.append(c)
        i += 1
    raise WhitelistError("VALUES 没有闭合")


def parse_dml(text: str) -> Dict[str, Script]:
    """交付 DML(scripts.sql)→ {脚本名: Script}。只收 scene='AGENT';同名出现两次直接报错(交付文件本身有问题)。"""
    out: Dict[str, Script] = {}
    for m in _INSERT.finditer(text):
        cols = [c.strip().strip('"') for c in m.group(1).split(",")]
        vals, _end = _values(text, m.end())
        if len(vals) != len(cols):
            raise WhitelistError("INSERT 列数 %d 与值数 %d 不符(附近:%s)" % (len(cols), len(vals), text[m.start():m.start() + 80]))
        row = dict(zip(cols, vals))
        if row.get("scene") != SCENE:
            continue
        name = row.get("script_name") or ""
        if name in out:
            raise WhitelistError("交付文件里 %s 出现了两次" % name)
        body = row.get("script_content") or ""
        out[name] = Script(name=name, md5=hashlib.md5(body.encode("utf-8")).hexdigest(),
                           params=param_keys(row.get("parameter_config")))
    if not out:
        raise WhitelistError("没有解析出任何 scene='AGENT' 的脚本")
    return out


def to_json(scripts: Mapping[str, Script]) -> List[dict]:
    return [{"name": s.name, "md5": s.md5, "params": [list(p) for p in s.params]} for s in sorted(scripts.values(), key=lambda x: x.name)]


def from_json(items: Iterable[Mapping]) -> Dict[str, Script]:
    return {i["name"]: Script(name=i["name"], md5=i["md5"], params=tuple(tuple(p) for p in i.get("params") or []))
            for i in items}


# -- 已登记的两种读法 ------------------------------------------------------------------------------

def from_rows(rows: Iterable[Tuple]) -> List[Registered]:
    """只读账号执行 EXPORT_SQL 得到的行:(script_name, content_md5, parameter_config, is_valid)。"""
    out = []
    for name, md5, params, valid in rows:
        out.append(Registered(name=str(name), md5=str(md5 or "").lower(), params=param_keys(params),
                              valid=str(valid).strip() in ("1", "True", "true", "t")))
    return out


def from_csv(text: str) -> List[Registered]:
    """白名单团队导出的 CSV(表头与 EXPORT_SQL 的列名一致;分隔符逗号或制表符)。"""
    sample = text[:2048]
    dialect = csv.Sniffer().sniff(sample, delimiters=",\t") if sample.strip() else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    need = {"script_name", "content_md5", "parameter_config", "is_valid"}
    if not reader.fieldnames or not need <= {f.strip().lower() for f in reader.fieldnames}:
        raise WhitelistError("导出文件的表头要有 %s(请按界面给出的 SELECT 导出)" % "、".join(sorted(need)))
    rows = [{k.strip().lower(): v for k, v in r.items()} for r in reader]
    return from_rows((r["script_name"], r["content_md5"], r["parameter_config"], r["is_valid"]) for r in rows)


# -- 比对 ------------------------------------------------------------------------------------------

def compare(expected: Mapping[str, Script], registered: Iterable[Registered]) -> Report:
    by: Dict[str, List[Registered]] = {}
    for r in registered:
        by.setdefault(r.name, []).append(r)
    rep = Report(expected=len(expected))
    for name in sorted(expected):
        want, got = expected[name], by.get(name, [])
        if not got:
            rep.findings.append(Finding(name, MISSING))
        elif len(got) > 1:
            rep.findings.append(Finding(name, DUPLICATE, "同名 %d 行:运行时只会用到其中一行" % len(got)))
        elif not got[0].valid:
            rep.findings.append(Finding(name, INVALID, "is_valid 不是 1"))
        elif got[0].md5 != want.md5:
            rep.findings.append(Finding(name, CONTENT, "md5 应为 %s,实为 %s" % (want.md5, got[0].md5)))
        elif got[0].params != want.params:
            rep.findings.append(Finding(name, PARAMS, "应为 %s,实为 %s" % (_fmt(want.params), _fmt(got[0].params))))
        else:
            rep.findings.append(Finding(name, OK))
    rep.extra = sorted(set(by) - set(expected))
    return rep


def _fmt(params: Tuple[Tuple[str, str], ...]) -> str:
    return "[%s]" % ", ".join("%s:%s" % p for p in params) if params else "[]"
