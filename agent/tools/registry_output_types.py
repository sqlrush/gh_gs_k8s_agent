#!/usr/bin/env python3
"""业务库脚本每一列的输出类型清单 —— 生成 tests/fixtures/registry_output_types.json(v1.5.2)。

    python3 tools/registry_output_types.py --host 127.0.0.1 --port 15434 --user agent_t \\
        --password-file ~/.og-test-pw --a-db adb --b-db bdb --write

**为什么要有这份清单。** 业务库可能是 M 兼容模式。现场中间件是 Java 写的,按 JDBC 类型映射取出每一列,再转成文字;
类型不在《GaussDB M-Compatibility 开发指南》JDBC 映射表里「取出来就是文字 / 整数 / 小数」那几种时,取回来的
可能是 ``[B@276fe535`` 这样的对象地址,原值丢失(2026-11-25 现场:布尔列)。脚本报不报错看不出这件事,
只能看每一列的类型 —— 而类型只有连上数据库才知道。

所以类型由本工具连测试库实测后写进清单(连同每条脚本正文的 sha256),守卫
``tests/test_registry_output_types_units.py`` 不连库,只核对三件事:清单覆盖全部业务库脚本、正文没改过
(改了就要重跑本工具)、每一列的类型都在安全集合里。

测试库要求:同一实例上一个 A 兼容库、一个 B 兼容库(openGauss 7 的 dolphin 插件,本地拿它代替 M 兼容库),
各灌一份含 schema ``app``、表 ``orders`` / ``customers``、函数 ``calc_total`` 的夹具(见 release 目录的 fixture.sql)。
账号要能读系统表与统计视图(SYSADMIN + MONADMIN)。只读:每条脚本在事务里执行后回滚,EXPLAIN 不带 ANALYZE 执行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

_AGENT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_AGENT))

from common.grmp import routing  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402

FIXTURE = _AGENT / "tests" / "fixtures" / "registry_output_types.json"

# 试跑参数:对象名取夹具里有的,让多数脚本真的返回行;其余按类型给安全值
PARAMS = {
    "limit": "50", "min_bytes": "0", "min_pages": "0", "min_rows": "0",
    "name": "calc_total", "names": "'orders','customers'", "order": "s.calls DESC", "package": "",
    "relname": "orders", "schema": "app", "schema_filter": "('pg_catalog','information_schema')",
    "schemas": "'app','public'", "sql": "SELECT id, amount FROM app.orders WHERE cust_id = 5 ORDER BY id",
    "tables": "'orders'", "columns": "'cust_id','amount'",
}
OVERRIDES = {
    "procinfo.locate_search": {"name": "calc", "package": "pkg"},
    "proctune.locate_search": {"name": "CALC", "package": "PKG_c"},
}


def sql_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(name: str, sql: str) -> str:
    vals = dict(PARAMS, **OVERRIDES.get(name, {}))
    return re.sub(r"\{\{(\w+)\}\}", lambda m: vals.get(m.group(1), "0"), sql).rstrip().rstrip(";")


def probe(conn, typnames, sql):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return [[c.name, typnames.get(c.type_code, str(c.type_code))] for c in (cur.description or [])], ""
    except Exception as exc:  # noqa: BLE001 —— 记下来:清单里写明这条在测试库上跑不了
        return [], str(exc).strip().splitlines()[0][:160]
    finally:
        conn.rollback()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-file", required=True, help="口令文件(只读第一行;不从命令行传口令)")
    ap.add_argument("--a-db", default="adb", help="A 兼容测试库")
    ap.add_argument("--b-db", default="bdb", help="B 兼容测试库(本地代替 M 兼容库)")
    ap.add_argument("--write", action="store_true", help=f"写入 {FIXTURE.relative_to(_AGENT)};不加只打印")
    args = ap.parse_args(argv)

    import psycopg2

    pw = pathlib.Path(args.password_file).expanduser().read_text(encoding="utf-8").splitlines()[0].strip()
    conns, typnames = {}, {}
    for key, db in (("a", args.a_db), ("b", args.b_db)):
        c = psycopg2.connect(host=args.host, port=args.port, user=args.user, password=pw, dbname=db)
        cur = c.cursor()
        cur.execute("SELECT datcompatibility::text FROM pg_database WHERE datname = current_database()")
        mode = cur.fetchone()[0]
        expect = "A" if key == "a" else "B"
        if mode != expect:
            print("库 %s 的兼容模式是 %s,应为 %s" % (db, mode, expect), file=sys.stderr)
            return 2
        cur.execute("SELECT oid, typname FROM pg_type")
        typnames[key] = {int(o): n for o, n in cur.fetchall()}
        conns[key] = c

    reg = Registry()
    out = {}
    for name in sorted(routing.BUSINESS_DB_SCRIPTS):
        rec = reg.find(name)
        sql = render(name, rec.script_content)
        entry = {"sql_sha256": sql_digest(rec.script_content)}
        for key in ("a", "b"):
            cols, err = probe(conns[key], typnames[key], sql)
            entry["columns_" + key] = cols
            if err:
                entry["error_" + key] = err
        out[name] = entry
        print("%-30s A:%s%s" % (name, ", ".join("%s=%s" % tuple(c) for c in entry["columns_a"]),
                                 ("  [A 报错:%s]" % entry["error_a"]) if "error_a" in entry else ""))
    if args.write:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("已写入", FIXTURE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
