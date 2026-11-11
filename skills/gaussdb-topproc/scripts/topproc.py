#!/usr/bin/env python3
"""topproc — top resource-consuming stored procedures/functions.

Port of internal/probe/topproc.go + internal/cli/topproc.go. Ranks
pg_stat_user_functions (function-level cumulative stats) by a whitelisted
sort key (--by). Degrades with a note when the view is empty (typically
track_functions=none).

Usage:
    topproc.py -c <conn> [--by time|self|calls] [--limit 20] [--format json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/ (repo root or install dir)
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break
        
for parent in _HERE.parents:
    if (parent / "common" / "sql.py").exists():
        sys.path.insert(0, str(parent))
        break
        
# SQL 已迁到 scripts/registry/topproc/ —— 两条路径共用同一份定义

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
# 结果值全是字符串：bool("f") 是 True、int("3704.0") 会抛异常。
# 类型还原一律走这里，不用裸 int()/float()/bool()。
from common.grmp.values import as_bool, as_float, as_int  # noqa: E402
import render  # noqa: E402

# Whitelisted --by values; ORDER BY clause is injected, so it MUST come from
# this map only (never from user input directly).
_SORT_COLS = {
    "time": "s.total_time DESC",
    "self": "s.self_time DESC",
    "calls": "s.calls DESC",
}
SORT_KEYS = ["time", "self", "calls"]

_EMPTY_NOTE = (
    "无函数级统计：函数级统计为空（多因函数级统计没开，track_functions=none），缺数据。"
    "需运维开启 track_functions（pl 或 all）后才能取到，再调用过程重采；或用 gaussdb-topsql 看顶层调用，"
    "以及（track_stmt_stat_level 捕获到的）过程内部语句。"
)

# 统计为空时**查清原因再说**(2026-11-11 现场):原来一律说「多因 track_functions=none」,模型照抄成事实;
# 实际那次是没切到业务库 —— pg_stat_user_functions 建在当前库的 pg_proc 上,在 postgres 库里看不到业务库的存储过程,
# 参数开没开都是空。三种情况动作不同:没切到业务库 / 参数关着 / 参数开着但还没有调用记录。
SETTINGS_SCRIPT = "config.settings"      # 取 track_functions 现值(全量 pg_settings,已在白名单里,不加新脚本)
_TOPSQL_TIP = "在那之前可用 gaussdb-topsql 看存储过程顶层调用(CALL 语句)的耗时"


def _track_functions(runner) -> str:
    """track_functions 的现值(实例级读数);取不到返回空串。"""
    try:
        rows = runner.run(SETTINGS_SCRIPT, {})
    except Exception:  # noqa: BLE001 —— 只为把空结果解释清楚,取不到就如实说取不到
        return ""
    for r in rows or []:
        if str(r.get("name", "")).strip().lower() == "track_functions":
            return str(r.get("setting", "")).strip().lower()
    return ""


def _where(runner) -> tuple[str, str]:
    """(这次在哪个业务库上查, 没切过去的原因)。库名为空 = 在实例的 postgres 库上查的。"""
    client = getattr(runner, "client", None)
    if client is None:                    # 直连:就在登录连的那个库上
        return "当前连接的库", ""
    target = getattr(client, "target_db", "") or ""
    try:
        if client.routes(TOP_PROCS_SCRIPT):
            return target, ""
    except Exception:  # noqa: BLE001
        pass
    if not target:
        return "", "会话没有切到业务库:登录时没给业务库,或登录写了「未直连」(原因见登录输出)"
    return "", ("行方白名单里这条脚本(%s)没有声明 dbName —— 增量白名单 scripts-delta-20261111.sql 还没导入"
                % TOP_PROCS_SCRIPT)


def empty_note(db: str, why_not: str, tf: str) -> str:
    tf_txt = "track_functions=%s" % tf if tf else "track_functions 现值没取到"
    if not db:
        tail = ("(函数级统计关着,切到业务库后也会为空,需运维评估开启,设为 pl 即可)" if tf == "none" else "")
        return ("函数级统计为空,但这次是在实例的 postgres 库上查的(%s)。函数统计按库各记一份,postgres 库里看不到业务库的存储过程,"
                "所以这里为空不能说明业务库没开统计,也不要据此说 track_functions 关着。要看某个业务库的存储过程:"
                "登录时带 --database <业务库>,登录写「已直连」后再查。参考:实例级 %s%s。"
                % (why_not, tf_txt, tail))
    if tf == "none":
        return ("函数级统计为空:已在「%s」上查,track_functions=none(函数级统计关着,这是 GaussDB 的默认值),所以没有数据。"
                "需运维评估后开启(设为 pl 即可;可只对该库 ALTER DATABASE … SET track_functions TO 'pl',开启后新建的连接才有统计),"
                "之后发生的调用才会被统计;%s。" % (db, _TOPSQL_TIP))
    if tf in ("pl", "all"):
        return ("函数级统计为空:已在「%s」上查,track_functions=%s 已开启,但还没有调用记录 —— 开启后这个库的存储过程还没被调用过,"
                "或统计被重置过;只对某个库设置时,开启前建好的连接(连接池长连接)不统计,要等连接重建。等业务调用一段时间后再查;%s。"
                % (db, tf, _TOPSQL_TIP))
    return ("函数级统计为空:已在「%s」上查;%s,无法区分是参数关着还是还没有调用记录。请 DBA 在该库执行 SHOW track_functions 确认:"
            "是 none(统计关着、缺数据)就需运维评估开启(pl 即可);%s。" % (db, tf_txt, _TOPSQL_TIP))


@dataclass(frozen=True)
class ProcStat:
    schema: str
    name: str
    calls: int
    total_ms: float
    self_ms: float


TOP_PROCS_SCRIPT = "topproc.top_procs"


def top_procs(runner, by: str, limit: int) -> tuple[list[ProcStat], str]:
    """Rank user functions/procedures; returns (rows, degrade_note).

    经统一入口取数。走中间件还是直连由连接的 driver 决定，这里不感知。
    """
    order = _SORT_COLS.get(by)
    if order is None:
        raise ValueError(f"--by {by!r}: must be one of {SORT_KEYS}")
    # order 落在 ORDER BY 位，类型校验对标识符位无效 ——
    # 取值只能来自上面那张白名单，绝不能直接来自用户输入。
    rows = runner.run(TOP_PROCS_SCRIPT, {"order": order, "limit": int(limit)})
    out = [
        ProcStat(r["nspname"], r["proname"], as_int(r["calls"]),
                 as_float(r["total_ms"]), as_float(r["self_ms"]))
        for r in rows
    ]
    if out:
        return out, ""
    db, why_not = _where(runner)
    return out, empty_note(db, why_not, _track_functions(runner))


def proc_table(by: str, rows: list[ProcStat], note: str) -> str:
    out = f"# Top Procedures by {by}\n\n"
    if not rows:
        return out + "> " + note + "\n"
    body = [[f"{r.schema}.{r.name}", str(r.calls), f"{r.total_ms:.2f}", f"{r.self_ms:.2f}"]
            for r in rows]
    out += render.table(["PROCEDURE", "CALLS", "TOTAL_MS", "SELF_MS"], body)
    out += ("\nNext: 只读诊断 → `gaussdb-procinfo <schema.proc>`；"
            "经验证优化 → `gaussdb-proctune <schema.proc>`。\n")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="topproc.py",
        description="Top resource-consuming stored procedures/functions")
    ap.add_argument("-c", "--conn", default="", help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--by", choices=SORT_KEYS, default="time", help="sort key")
    ap.add_argument("--limit", type=int, default=20, help="max rows")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    ap.add_argument("--timeout", type=int, default=None)
    args = ap.parse_args(argv)
    cli.apply_session_arg(args)

    try:
        runner = access.for_conn(args.conn, timeout=args.timeout)
    except (common.ConfigError, common.CredentialError, access.AccessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        rows, note = top_procs(runner, args.by, args.limit)
        if args.format == "json":
            print(json.dumps({"rows": [r.__dict__ for r in rows], "note": note},
                             ensure_ascii=False, indent=2))
        else:
            print(proc_table(args.by, rows, note), end="")
        return 0
    except (ValueError, KeyError, common.DBError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:          # 渲染/协议层的失败也要清楚报出来
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
