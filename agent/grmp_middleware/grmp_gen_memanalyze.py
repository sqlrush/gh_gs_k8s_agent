"""为 memanalyze 生成**按实例定制**的 registry 脚本。

memanalyze 与其他 skill 不同：它的 SQL 正文取决于目标实例有哪些视图、
每个视图有哪些列 —— 探测到什么就查什么，缺的列补 NULL。

    instance     gs_total_memory_detail / pv_total_memory_detail / dbe_perf...
    session_ctx  gs_session_memory_detail / pv_session_memory_detail
    ...共 10 个槽位，各有 1~3 个候选视图

固定白名单覆盖不了这种形态：同一条逻辑查询在不同实例上是不同的 SQL。

解法是把「脚本名」和「脚本正文」分开：
  · 脚本名固定（memanalyze.instance 等），skill 代码按名字调用，不感知实例差异
  · 脚本正文按目标实例生成 —— 交付时对客户实例跑一次本工具，
    产出的 YAML 与 DML 才是那套环境专用的

用法：
    python3 -m grmp_middleware.grmp_gen_memanalyze --conn og
    python3 -m grmp_middleware.grmp_gen_memanalyze --conn og --out scripts/registry/memanalyze

⚠️ 换一套实例就要重新生成。拿 A 实例生成的脚本去 B 实例跑，
   轻则报列不存在（会被降级逻辑接住），重则查到语义不同的列。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / \
    "skills" / "gaussdb-memanalyze" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import common  # noqa: E402
import probe  # noqa: E402

DEFAULT_OUT = "scripts/registry/memanalyze"

# 与 collectors.py / wlm.py 里的常量保持一致
INSTANCE_COLS = ("memorytype", "memorymbytes")
CTX_COLS = ("contextname", "totalsize", "freesize", "usedsize")
SESS_COLS = ("sessid", "init_mem", "used_mem", "peak_mem")
ACT_COLS = ("sessionid", "pid", "usename", "application_name", "state", "query")
SQL_COLS = ("queryid", "query", "start_time", "duration", "estimate_memory",
            "used_memory", "max_peak_memory", "average_peak_memory", "spill_info")
OP_COLS = ("queryid", "plan_node_id", "plan_node_name", "duration",
           "estimate_memory", "memory_used", "max_peak_memory",
           "average_peak_memory", "spill_size", "warning")

HEADER = """\
# ⚠️ 本文件由 grmp_middleware/grmp_gen_memanalyze.py 生成，请勿手工编辑。
#
# memanalyze 的 SQL 正文取决于目标实例有哪些视图、每个视图有哪些列 ——
# 探测到什么就查什么，缺的列补 NULL。固定白名单覆盖不了这种形态，
# 所以脚本名固定、正文按实例生成。
#
# 生成依据（来自实例 %s）：
#   视图  %s
#   列    %s
#
# ⚠️ 换一套实例必须重新生成。
"""


def _emit(out: pathlib.Path, fname: str, name: str, desc: str,
          conn_name: str, view: str, cols: str, sql: str, params=()) -> None:
    body = [HEADER % (conn_name, view, cols),
            "name: %s" % name,
            "description: %s" % desc,
            "sql: |"]
    body += ["  " + line for line in sql.strip().splitlines()]
    if params:
        body.append("params:")
        for key, typ, pdesc in params:
            body += ["  - key: %s" % key, "    type: %s" % typ,
                     "    description: %s" % pdesc]
    (out / fname).write_text("\n".join(body) + "\n", encoding="utf-8")


def generate(conn_name: str, out: pathlib.Path) -> list:
    db = common.Database.connect(conn_name)
    try:
        cat = probe.probe_views(db)
    finally:
        db.close()

    out.mkdir(parents=True, exist_ok=True)
    made = []

    def add(fname, name, desc, slot, sql_fn, params=()):
        vi = cat.get(slot)
        if not vi.available:
            made.append((name, None, "槽位 %s 不可用：%s" % (slot, vi.reason)))
            return
        _emit(out, fname, name, desc, conn_name, vi.name,
              ", ".join(vi.columns), sql_fn(vi), params)
        made.append((name, vi.name, "%d 列" % len(vi.columns)))

    add("instance.yaml", "memanalyze.instance", "实例级内存分布", "instance",
        lambda vi: "SELECT %s FROM %s;"
                   % (probe.columns_expr(vi, INSTANCE_COLS), vi.name))

    ctx_slot = "session_ctx" if cat.has("session_ctx") else "thread_ctx"
    add("context.yaml", "memanalyze.context", "内存上下文占用 Top N", ctx_slot,
        lambda vi: ("SELECT contextname, sum(totalsize) AS totalsize,\n"
                    "       sum(freesize) AS freesize, sum(usedsize) AS usedsize\n"
                    "FROM (SELECT %s FROM %s) t\n"
                    "GROUP BY contextname ORDER BY 4 DESC NULLS LAST\n"
                    "LIMIT {{limit}};"
                    % (probe.columns_expr(vi, CTX_COLS), vi.name)),
        (("limit", "INTEGER", "返回条数上限"),))

    add("session.yaml", "memanalyze.session", "会话级内存 Top N", "session_mem",
        lambda vi: ("SELECT %s FROM %s\n"
                    "ORDER BY %s DESC NULLS LAST LIMIT {{limit}};"
                    % (probe.columns_expr(vi, SESS_COLS), vi.name,
                       "peak_mem" if probe.has_col(vi, "peak_mem") else "used_mem")),
        (("limit", "INTEGER", "返回条数上限"),))

    add("activity.yaml", "memanalyze.activity", "会话在跑什么（用于关联）", "activity",
        lambda vi: "SELECT %s FROM %s;"
                   % (probe.columns_expr(vi, ACT_COLS), vi.name))

    for slot, fname, name, desc, cols in (
        ("wlm_session", "wlm_sql.yaml", "memanalyze.wlm_sql",
         "SQL 级资源跟踪（实时）", SQL_COLS),
        ("wlm_session_hist", "wlm_sql_hist.yaml", "memanalyze.wlm_sql_hist",
         "SQL 级资源跟踪（历史）", SQL_COLS),
        ("wlm_operator", "wlm_operator.yaml", "memanalyze.wlm_operator",
         "算子级资源跟踪（实时）", OP_COLS),
        ("wlm_operator_hist", "wlm_operator_hist.yaml",
         "memanalyze.wlm_operator_hist", "算子级资源跟踪（历史）", OP_COLS),
    ):
        add(fname, name, desc, slot,
            lambda vi, c=cols: ("SELECT %s FROM %s%s LIMIT {{limit}};"
                                % (probe.columns_expr(vi, c), vi.name,
                                   _order_by(vi))),
            (("limit", "INTEGER", "返回条数上限"),))

    return made


def _order_by(vi) -> str:
    """与 wlm.py 的 _order_by 同源：优先按峰值内存排序，没有该列就不排。"""
    if probe.has_col(vi, "max_peak_memory"):
        return "\nORDER BY max_peak_memory DESC NULLS LAST"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="为 memanalyze 生成按实例定制的 registry 脚本")
    ap.add_argument("--conn", required=True, help="目标实例的连接名")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    out = pathlib.Path(args.out).expanduser()
    try:
        made = generate(args.conn, out)
    except (common.ConfigError, common.CredentialError, common.DBError) as exc:
        print("探测失败：%s" % exc, file=sys.stderr)
        return 2

    print("按实例 %r 生成到 %s：" % (args.conn, out))
    for name, view, note in made:
        if view is None:
            print("  跳过  %-28s %s" % (name, note))
        else:
            print("  生成  %-28s %-34s %s" % (name, view, note))
    print("\n⚠️ 换一套实例必须重新跑本工具 —— 脚本正文与实例强绑定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
