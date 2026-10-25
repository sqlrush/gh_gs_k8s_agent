"""审计:每条注册脚本的 {{占位符}} 声明类型,与它在 SQL 里的实际用法是否自洽。

背景:2026-08 现场出过一次同类问题——占位符兜底填 'test' 撞上整数列,
报 `invalid input syntax for integer: "test"`,同一条 SQL 反复失败一小时。
类型声明与用法不一致是这类故障的根,所以单独拉一条检查。

判据:
  · INTEGER / BOOLEAN 的占位符若被单引号包住 → 会变成字符串字面量,撞整数列;
  · STRING 的占位符若**没有**被单引号包住 → 取值必须自带引号(registry 里
    IN ({{tables}}) 这类是有意为之,注释里写明"取值只给带引号的字面量列表")。
两类都只报出来给人看,不自动判错。
"""
from __future__ import annotations

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "registry"


def audit():
    quoted_ok, bare_string, numeric_quoted, total = [], [], [], 0
    for path in sorted(ROOT.rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sql = doc.get("sql") or ""
        types = {p["key"]: p.get("type", "STRING")
                 for p in (doc.get("params") or []) if isinstance(p, dict)}
        for key, typ in types.items():
            total += 1
            for m in re.finditer(r"\{\{\s*%s\s*\}\}" % re.escape(key), sql):
                s, e = m.start(), m.end()
                quoted = sql[max(0, s - 1):s] == "'" and sql[e:e + 1] == "'"
                rel = "%s/%s" % (path.parent.name, path.name)
                if typ in ("INTEGER", "BOOLEAN") and quoted:
                    numeric_quoted.append((rel, key, typ))
                elif typ == "STRING" and not quoted:
                    bare_string.append((rel, key, sql[max(0, s - 34):e + 6].replace("\n", " ").strip()))
                else:
                    quoted_ok.append((rel, key, typ))
    return quoted_ok, bare_string, numeric_quoted, total


def main() -> int:
    ok, bare, numq, total = audit()
    print("占位符总数:%d(自洽 %d)" % (total, len(ok)))
    print()
    print("【严重】数值/布尔却被单引号包住 —— 会撞整数列:%d 处" % len(numq))
    for rel, key, typ in numq:
        print("   %-34s {{%s}} 声明 %s" % (rel, key, typ))
    print()
    print("【需人工确认】STRING 未被引号包住 —— 取值必须自带引号:%d 处" % len(bare))
    for rel, key, ctx in bare:
        print("   %-34s {{%s}}  上下文:%s" % (rel, key, ctx[:56]))
    return 1 if numq else 0


if __name__ == "__main__":
    sys.exit(main())
