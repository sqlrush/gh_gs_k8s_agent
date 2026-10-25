#!/usr/bin/env python3
"""slowsql — list statements slower than a threshold (avg ms).

Port of internal/probe/slowsql.go + internal/cli/slowsql.go. Reads
dbe_perf.statement aggregates; slowest first. cpu_sec is captured (JSON) to
expose the DB-time trap (slow-but-low-CPU = contention, not CPU-bound work).

Usage:
    slowsql.py -c <conn> [--threshold 1000] [--limit 20] [--format json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
import os
import csv

_HERE = pathlib.Path(__file__).resolve()
ROOT_DIR = _HERE.parents[3]
sys.path.insert(0, str(_HERE.parent))  # sibling modules
for _anc in _HERE.parents:  # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common import syssql  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402
import render  # noqa: E402

SLOWSQL_MAX_ROWS = 20
# slowsql.slow_sql 在 SQL 里把正文截到 180 字(LEFT(…, 180)):够不到 FROM 的长语句要按 sql_id 补全文才能判
QUERY_SQL_CHARS = 180


@dataclass(frozen=True)
class StmtRow:
    sql_id: str
    query: str
    calls: int
    avg_ms: float
    total_sec: float
    cpu_sec: float
    rows: int


# 脚本 scripts/registry/slowsql/slow_sql.yaml 的 SELECT 列序。
# 下游按 r[0]..r[6] 取值，两者必须对齐。
SLOW_SQL_COLUMNS = (
    "unique_sql_id", "query", "calls", "avg_ms", "total_sec", "cpu_sec", "rows",
)

SLOW_SQL_SCRIPT = "slowsql.slow_sql"


def fetch_rows(runner, threshold_ms: int, limit: int, begin_time: str) -> list[tuple]:
    """经统一入口取数，摊成位置元组。

    按**逻辑名**调用而不是脚本 ID：ID 是环境相关数据，硬编码后换环境
    仍然存在但指向另一条脚本，表现为执行成功、结果无关、不报错。

    两条路径（中间件 / 直连）返回的都是全字符串化的行字典，所以这里
    直接用 r[col] 而不是 r.get(col)：脚本 SELECT 列变了而这里没跟上时
    要当场 KeyError，用 get 兜底会让缺失列静默变成 None，报错点被推迟到
    格式化环节，排查方向被带偏。
    """
    return [
        tuple(row[col] for col in SLOW_SQL_COLUMNS)
        for row in runner.run(
            SLOW_SQL_SCRIPT,
            {
                "threshold_ms": int(threshold_ms),
                "limit": int(limit),
                "begin_time": str(begin_time),
            },
        )
    ]


def slow_sql(runner, threshold_ms: int, limit: int, begin_time: str, export: bool,
             include_system: bool = False) -> list[StmtRow]:
    return slow_sql_screened(runner, threshold_ms, limit, begin_time, export, include_system)[0]


def slow_sql_screened(runner, threshold_ms: int, limit: int, begin_time: str, export: bool,
                      include_system: bool = False) -> tuple[list[StmtRow], "syssql.Screening"]:
    """(慢 SQL 行, 排除内核系统 SQL 的结果)。

    2026-10-22 客户要求:慢 SQL 定位排除 GaussDB 自己的系统 SQL(WDR 快照、监控采集、诊断工具查系统视图),
    报告里写明排除了几条。include_system=True(--include-system)原样保留。
    """
    fetched = fetch_rows(runner, threshold_ms, limit if include_system else limit + syssql.SCREEN_MARGIN,
                         begin_time)
    if include_system:
        screening = syssql.keep_all(fetched)
    else:
        screening = syssql.screen(fetched, want=limit, sql_id=lambda r: r[0], text=lambda r: r[1],
                                  shown_chars=QUERY_SQL_CHARS, full_text=syssql.full_text_lookup(runner),
                                  seconds=lambda r: as_float(r[4]), cache=syssql.verdict_cache())
    return _summarize(screening.kept, export), screening


def _summarize(rows: list[tuple], export: bool) -> list[StmtRow]:
    """排除之后的行 → 展示行;按需导出 CSV(原 slow_sql 的后半段,逻辑未改)。"""
    # === 新增逻辑：rows > 20 时保存为 CSV 文件 ===
    if len(rows) > SLOWSQL_MAX_ROWS or export:
        # 暂时关闭导出，仅当export开启时
        if export:
            # 定义 CSV 表头
            headers = ['unique_sql_id', 'query', 'calls', 'avg_ms', 'total_sec',
                       'cpu_sec', 'rows']

            # 写入 CSV 文件。
            #
            # **G_EXPORT_DIR 必须先转成 Path。** os.environ.get 给的是 str，
            # 而后面还要 `/ "csv"` —— str / str 直接 TypeError，也就是说这个
            # 环境变量一旦设置，导出必崩。实测确认过。
            g_export_dir = os.environ.get('G_EXPORT_DIR')
            base = pathlib.Path(g_export_dir) if g_export_dir else _HERE.parents[3] / "tmp"
            csv_dir = base / "csv"
            try:
                csv_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                # 建不出目录就**不要往下走**。原先只 print 一句再继续 open()，
                # 报出来的是「No such file or directory: .../xxx.csv」，
                # 看着像取数失败，而真正的原因（目录建不了）已经被那句 print
                # 冲到几十行之前去了。
                print(f"导出目录 {csv_dir} 建不出来：{e}；本次不导出 CSV。",
                      file=sys.stderr)
                return [StmtRow(r[0], r[1], as_int(r[2]), as_float(r[3]),
                                as_float(r[4]), as_float(r[5]), as_int(r[6]))
                        for r in rows[:min(SLOWSQL_MAX_ROWS, len(rows), 5)]]

            csv_filename = csv_dir / f"slow_sql_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for r in rows:
                    writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6]])
                print(f"数据已导出到: {csv_filename} (共 {len(rows)} 行)")
        last_index = min(SLOWSQL_MAX_ROWS, len(rows), 5)
        return [StmtRow(r[0], r[1], as_int(r[2]), as_float(r[3]), as_float(r[4]),
                        as_float(r[5]), as_int(r[6])) for r in rows[:last_index]]
    else:
        return [StmtRow(r[0], r[1], as_int(r[2]), as_float(r[3]), as_float(r[4]),
                        as_float(r[5]), as_int(r[6])) for r in rows]


def stmt_table(title: str, rows: list[StmtRow]) -> str:
    if not rows:
        return (f"## {title}\n\nNo matching statements. "
                f"Check `enable_stmt_track` / lower --threshold.\n")
    body = [[str(i + 1), r.sql_id, str(r.calls), f"{r.avg_ms:.2f}",
             f"{r.total_sec:.2f}", str(r.rows), render.truncate(r.query, 100)]
            for i, r in enumerate(rows)]
    return ("## " + title + "\n\n" +
            render.table(["#", "SQL_ID", "CALLS", "AVG_MS", "TOTAL_S", "ROWS", "QUERY"], body) +
            "\n> QUERY 只截到 100 字。要看某条的全文或执行计划，"
            "直接说「看 sql_id <SQL_ID> 的执行计划」。\n")


def build_parser() -> argparse.ArgumentParser:
    """参数解析器单独拆出来，好让测试能直接检验它。

    原先它长在 main() 里，于是 test_export_flag_rejects_the_bool_string_trap
    只能 skip —— 一个**结构上永远跑不起来**的守卫。那比没有守卫更糟：
    它在测试报告里显示成「跳过」，看着像覆盖，实际上就算有人把 --export
    改成 type=bool，它也不会red。
    """
    ap = argparse.ArgumentParser(prog="slowsql.py",
                                 description="List statements slower than --threshold (avg ms)")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    seven_days_ago = datetime.now() - timedelta(days=7)
    begin_time_str = seven_days_ago.strftime('%Y-%m-%d %H:%M:%S')
    ap.add_argument("--threshold", type=int, default=1000, help="avg elapsed threshold (ms)")
    ap.add_argument("--limit", type=int, default=20, help="max rows")
    ap.add_argument("--begin_time", type=str, default=begin_time_str, help="execution begin time")
    # 不用 type=bool：那就是 bool(str)，非空字符串一律为真，于是
    # `--export false` 反而打开了导出。与 bool("f") 是同一个坑。
    ap.add_argument("--export", action="store_true",
                    help="即使行数不多也导出 CSV")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--include-system", action="store_true",
                    help="不排除内核系统 SQL(默认排除只访问系统表 / 系统视图 / 系统函数的语句,并写明排除了几条)")
    # 仅直连路径生效：GRMP 协议没有超时字段，走中间件时超时由服务端决定
    ap.add_argument("--timeout", type=int, default=None)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cli.apply_session_arg(args)
    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        rows, screening = slow_sql_screened(runner, args.threshold, args.limit, args.begin_time,
                                            args.export, args.include_system)
        note = screening.note()
        if args.format == "json":
            print(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=2))   # stdout 形状不变
            if note:
                print(note, file=sys.stderr)
        elif screening.all_excluded():
            print("## Slow SQL\n\n%s。\n" % screening.all_excluded_text(
                args.limit + syssql.SCREEN_MARGIN, "平均耗时超过 %d ms 的" % args.threshold))
            print("> %s要连系统 SQL 一起看,加 `--include-system`。\n" % note)
        else:
            print(stmt_table("Slow SQL", rows), end="")
            if note:
                print("\n> %s要连系统 SQL 一起看,加 `--include-system`。\n" % note)
        return 0
    except (ValueError, KeyError, common.DBError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 渲染/协议层的失败也要清楚地报出来
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
