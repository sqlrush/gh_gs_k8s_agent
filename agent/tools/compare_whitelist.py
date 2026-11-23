#!/usr/bin/env python3
"""行方白名单核对(2026-11-12):白名单团队把中间件里的脚本导出成 CSV,与本版交付的全量 DML 逐条比对。

为什么要有:行方 11-12 的分析报告把「增量没导入」当成了代码问题(health.storage 早在 20261108 增量里改过、
11 条 dbName 问题是 20261111 没导入)。行方白名单的真实状态我们看不到,只能靠这份导出核对,不再靠猜。

列出:缺哪几条、哪几条正文 / 参数定义 / 执行库不一致、哪几条没生效或重名,并按这些脚本反查「该导哪几份增量」。
比对口径复用发版台的白名单关卡(release/whitelist.py),两边结论一致。只读,不连任何库。

导出 SQL(白名单团队在中间件的配置库执行,导成 CSV,分隔符逗号或制表符;database_type 列可选,有就一起比):
    SELECT script_name, md5(script_content) AS content_md5, parameter_config, is_valid, database_type
      FROM grmp.script_config WHERE scene = 'AGENT' ORDER BY script_name;

用法: compare_whitelist.py <导出.csv> [--delivery <docs/delivery 目录>]
退出码:0 = 全部一致;1 = 有不一致(清单打印在标准输出);2 = 输入有问题。
"""
from __future__ import annotations

import argparse
import csv
import io
import pathlib
import re
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from release import whitelist as wl  # noqa: E402

EXPORT_SQL = ("SELECT script_name, md5(script_content) AS content_md5, parameter_config, is_valid, database_type\n"
              "  FROM grmp.script_config WHERE scene = 'AGENT' ORDER BY script_name;")
_INSERT_NAME = re.compile(r"script_config_seq\.nextval,\s*'SQL',\s*'([a-z_]+\.[a-z_0-9]+)',\s*'(postgres|appbusiness)'")
_DELTA = re.compile(r"^scripts-delta-(\d{8})\.sql$")


def dbtypes_from_dml(text: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _INSERT_NAME.finditer(text)}


def dbtypes_from_csv(text: str) -> Dict[str, str]:
    """导出里带了 database_type 列就取,没带返回空(只比正文与参数)。"""
    sample = text[:2048]
    dialect = csv.Sniffer().sniff(sample, delimiters=",\t") if sample.strip() else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames or "database_type" not in {f.strip().lower() for f in reader.fieldnames}:
        return {}
    out = {}
    for r in reader:
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items()}
        out[r.get("script_name", "")] = r.get("database_type", "") or "postgres"
    return out


def delta_files(delivery: pathlib.Path, names: Set[str]) -> List[Tuple[str, Set[str]]]:
    """(增量文件, 其中涉及的本版脚本) 按日期从早到晚。"""
    out = []
    for p in sorted(delivery.glob("scripts-delta-*.sql")):
        if _DELTA.match(p.name):
            text = p.read_text(encoding="utf-8")
            out.append((p.name, {n for n in names if "'%s'" % n in text}))
    return out


def advise(bad: Set[str], deltas: Sequence[Tuple[str, Set[str]]]) -> Tuple[List[str], Set[str]]:
    """(该导的增量文件,从早到晚;增量里都没有、只能按全量重导的脚本)。"""
    files = [f for f, ns in deltas if ns & bad]
    covered = set().union(*(ns for _f, ns in deltas)) if deltas else set()
    return files, bad - covered


def run(csv_text: str, delivery: pathlib.Path, out=sys.stdout) -> int:
    full = (delivery / "scripts.sql").read_text(encoding="utf-8")
    expected = wl.parse_dml(full)
    try:
        registered = wl.from_csv(csv_text)
    except wl.WhitelistError as exc:
        print("导出文件有问题:%s\n请按这条 SQL 导出:\n%s" % (exc, EXPORT_SQL), file=out)
        return 2
    rep = wl.compare(expected, registered)
    want_t, got_t = dbtypes_from_dml(full), dbtypes_from_csv(csv_text)
    ok_names = {f.name for f in rep.findings if f.status == wl.OK}
    dbt_diff = sorted(n for n in ok_names if n in got_t and n in want_t and got_t[n] != want_t[n])

    counts = rep.counts()
    print("本版应有 %d 条;中间件导出 %d 行。" % (rep.expected, len(registered)), file=out)
    print("、".join("%s %d 条" % (wl.LABELS[k], v) for k, v in counts.items() if v), file=out)
    if not got_t:
        print("(导出里没有 database_type 列,执行库没有比对;建议按文件头的 SQL 带上这一列重导)", file=out)
    for f in rep.problems:
        print("  - %s:%s%s" % (f.name, wl.LABELS[f.status], (" —— " + f.detail) if f.detail else ""), file=out)
    for n in dbt_diff:
        print("  - %s:执行库不一致 —— 应为 %s,实为 %s" % (n, want_t[n], got_t[n]), file=out)
    if rep.extra:
        print("中间件里有、本版不再使用的 %d 条(可保留,智能体不会调用):%s" % (len(rep.extra), "、".join(rep.extra)), file=out)

    bad = {f.name for f in rep.problems} | set(dbt_diff)
    if not bad:
        print("结论:与本版一致,不用导入。", file=out)
        return 0
    files, uncovered = advise(bad, delta_files(delivery, set(expected)))
    print("结论:有 %d 条与本版不一致。" % len(bad), file=out)
    if files:
        print("按日期从早到晚依次导入:%s(行方手工改过的记录以增量文件为准覆盖)。" % "、".join(files), file=out)
    if uncovered:
        print("另有 %d 条不在任何增量里(多半是手工改过或很早的版本),请从全量 scripts.sql 里取这几条重导:%s"
              % (len(uncovered), "、".join(sorted(uncovered))), file=out)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("export_csv")
    ap.add_argument("--delivery", default=str(ROOT / "docs" / "delivery"))
    a = ap.parse_args(argv)
    try:
        text = pathlib.Path(a.export_csv).read_text(encoding="utf-8-sig")
    except OSError as exc:
        print("读不了导出文件:%s" % exc, file=sys.stderr)
        return 2
    return run(text, pathlib.Path(a.delivery))


if __name__ == "__main__":
    raise SystemExit(main())
