#!/usr/bin/env python3
"""config — 只读的运行参数体检（openGauss / GaussDB）。

取全量 `pg_settings`，与基线比对，只报**客观上就是错的**那几类：自相矛盾、
会静默丢数据/丢可观测性、按实例自己的内存预算算出来的超卖。

**它不是「参数推荐值大全」。** 绝大多数性能参数的合理值取决于机器规格、业务形态
和数据量，而这个工具看不到 CPU 核数、物理内存和磁盘类型 —— 对着看不到的事实给
"建议值"就是编依据。设计上的取舍写在 scripts/audit.py 的模块说明里。

只读：从不执行 `ALTER SYSTEM` / `SET`，所有处置都给 DBA 人工执行。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Optional

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # sibling modules
for _anc in _HERE.parents:                      # locate common/
    if (_anc / "common" / "__init__.py").exists():
        sys.path.insert(0, str(_anc))
        break

import common  # noqa: E402
from common import access  # noqa: E402
from common import cli  # noqa: E402
from common import reports  # noqa: E402
from common.finding import findings_to_json  # noqa: E402

import audit  # noqa: E402
import baseline  # noqa: E402
import render  # noqa: E402

SKILL = "gaussdb-config"
SETTINGS_SCRIPT = "config.settings"


def collect(runner) -> tuple:
    """(settings 字典, findings)。"""
    rows = runner.run(SETTINGS_SCRIPT, {})
    st = baseline.to_settings(rows)
    return st, audit.audit(st)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="config.py",
        description="运行参数体检（只读，只报客观错误，不给凭空的推荐值）")
    ap.add_argument("-c", "--conn", default="",
                    help="连接名（省略则用 gaussdb-login 建立的会话）")
    cli.add_session_arg(ap)
    ap.add_argument("--show", default="",
                    help="额外原样列出这些参数（逗号分隔），用于人工核对")
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
        st, findings = collect(runner)
    except (common.DBError, access.QueryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        conn_name = common.config.resolved_name(args.conn)
        if args.format == "json":
            print(findings_to_json(findings, skill=SKILL))
        else:
            wanted = [x.strip() for x in args.show.split(",") if x.strip()]
            print(render.report(st, findings, wanted), end="")
        payload = {"conn": conn_name,
                   "findings": [f.to_dict() for f in findings],
                   "settings_count": len(st)}
        saved = reports.archive("config", payload, instance=conn_name)
        reports.announce("config", saved, conn_name)
        return 0
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
