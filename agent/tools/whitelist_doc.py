#!/usr/bin/env python3
"""生成《白名单脚本清单》(docs/delivery/白名单脚本清单.md):与 docs/delivery/scripts.sql(行方实际导入的全量)一一对应。

    python3 agent/tools/whitelist_doc.py            # 在仓库根运行

SQL 正文取自 scripts.sql 本身(导什么就列什么),用途与参数说明取自 agent/scripts/registry;两边正文逐条核对一致,
不一致直接报错。2026-11-05 起在本仓库维护(原先是仓库外的 ~/.deploy-doc/gen_whitelist.py)。加了增量文件,在下面
DELTAS 里按「最新的在前」补一行说明。
"""
import collections
import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[2]
sql_text = (ROOT / "docs/delivery/scripts.sql").read_text(encoding="utf-8")

# 只在 A 兼容库适用的脚本(v1.5.1):M 兼容库没有对应的系统视图,技能在 M 库自动不调用
ONLY_A = {n: "仅 A 兼容库。M 兼容库没有 ADM_SOURCE 视图（只继承了部分系统视图），技能在 M 兼容库上自动不调用这条脚本"
          for n in ("procinfo.source_adm", "proctune.source_adm")}

# 增量文件:(文件名, 说明),最新的在前
DELTAS = [
    ("scripts-delta-20261123.sql", "已导入 132 条（v1.5.0）升级到 v1.5.1：13 条更新（M 兼容业务库上报错的分区体检、表写入计数、统计信息过期、"
                                   "SQL 审查的表 / 索引清单、过程模糊查找等改成 A / M 两种兼容模式都能执行的写法；登录探针多取兼容模式）；"
                                   "导入后仍是 132 条。更早的版本先按顺序导到 20261119"),
    ("scripts-delta-20261119.sql", "已导入 132 条（v1.4.14）升级到 v1.5.0：4 条更新（锁堵塞成对明细、统计信息过期、未使用的索引、内核函数探测的拼接改用 concat，修 M 兼容业务库上 || 不是拼接、整条报错）；导入后仍是 132 条。更早的版本先按顺序导到 20261116"),
    ("scripts-delta-20261116.sql", "已导入 132 条（v1.4.13）的升级：2 条更新（死元组清单、锁堵塞成对明细的时间差改用 date_part('epoch', clock_timestamp() - 列)，修 M 兼容业务库上报 un support type）；导入后仍是 132 条。"
                                   "还在 138 条的先导 20261112"),
    ("scripts-delta-20261112.sql", "已导入 138 条（v1.4.11 / v1.4.12）的升级：2 条新增（SQL 级内存改从执行历史取、WDR 写入来源改读实时累计，都是视图没数据时的退路）。另有 8 条两段 SQL 的脚本不再使用（行方中间件拦截），可保留不删"),
    ("scripts-delta-20261111.sql", "已导入 137 条（v1.4.7 ~ v1.4.10）的升级：1 条新增（登录核实是否已切到业务库）+ 71 条更新执行库"
                                   "（database_type 与 parameter_config 里的 dbName，中间件按 dbName 切到业务库）；导入后 138 条。"
                                   "还在 133 条的先导 20261105、再导 20261108"),
    ("scripts-delta-20261108.sql", "已导入 137 条（v1.4.7 ~ v1.4.9）的升级：4 条更新（健康检查 / 慢 SQL 的 Slow SQL、Storage、Top SQL）"),
    ("scripts-delta-20261105.sql", "已导入 128 条（v1.4.3 ~ v1.4.6）的升级：9 条新增 + 2 条更新（xlog 写入来源、卡住回收的事务、"
                                   "按写入行数排 Top SQL），带用途说明与导入后自检"),
    ("scripts-delta-20261030.sql", "已导入 126 条（v1.4.0 ~ v1.4.2）的升级：2 条新增（存储过程源码的 ADM_SOURCE 兜底）；之后再导 20261105 那份"),
    ("scripts-delta-20261024.sql", "已导入 123 条（v1.3.x）的升级：12 条更新，带修改原因与导入后自检"),
    ("scripts-delta-20261026.sql", "接着上一份导：3 条新增（SQL 优化的现状与趋势）；之后再导 20261030 那份"),
    ("scripts-delta-20261022.sql", "更早版本（118 条）的升级：5 条新增，先于上面两份导"),
    ("scripts-delta-20261018.sql", "更早版本（105 条）的升级：13 条新增 + 9 条更新，最先导"),
]

def parse_inserts(text):
    """逐条解析 INSERT ... VALUES (...);只认本文件的固定列序。返回 [(name, content, params_json)]。"""
    out, i = [], 0
    head = "VALUES (grmp.script_config_seq.nextval, "
    while True:
        j = text.find(head, i)
        if j < 0:
            break
        k = j + len(head)
        vals = []
        while True:                      # 读到 22 个值或右括号
            while text[k] in " \n":
                k += 1
            if text[k] == "'":
                k += 1; buf = []
                while True:
                    if text[k] == "'" and text[k + 1] == "'":
                        buf.append("'"); k += 2
                    elif text[k] == "'":
                        k += 1; break
                    else:
                        buf.append(text[k]); k += 1
                vals.append("".join(buf))
            else:
                m = re.match(r"[^,)]+", text[k:]); vals.append(m.group(0).strip()); k += m.end()
            while text[k] in " \n":
                k += 1
            if text[k] == ",":
                k += 1; continue
            if text[k] == ")":
                break
        # 列序:script_type, script_name, database_type, refered_appbusiness, kernel_version, region,
        #      deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene ...
        out.append((vals[1], vals[9], vals[10], vals[11], vals[2]))
        i = k
    return out

rows = parse_inserts(sql_text)
reg = {}
for f in (ROOT / "agent/scripts/registry").glob("*/*.yaml"):
    d = yaml.safe_load(f.read_text(encoding="utf-8"))
    reg[d["name"]] = d
N = len(rows)
assert N >= 126, N
mism = [n for n, c, _, _, _ in rows if n not in reg or reg[n]["sql"].strip() != c.strip()]
dbt = [n for n, _, _, _, t in rows if n in reg and (reg[n].get("database_type") or "postgres") != t]
assert not dbt, ("执行库(database_type)与 registry 不一致:", dbt[:10])
assert not mism, ("正文不一致或 registry 缺:", mism[:10])
assert all(s == "AGENT" for _, _, _, s, _ in rows)

groups = collections.OrderedDict()
for n, c, p, _, t in sorted(rows, key=lambda r: r[0]):
    groups.setdefault(n.split(".")[0], []).append((n, c, json.loads(p or "[]"), t))

SKILL = {"config": "参数体检", "explain": "执行计划", "health": "健康检查", "lockwait": "锁等待", "memanalyze": "内存分析",
         "perf": "性能概览", "procinfo": "存储过程信息", "proctune": "存储过程调优", "session": "会话", "slowsql": "慢 SQL",
         "sqlfetch": "SQL 全文", "sqlreview": "SQL 审核", "sqltune": "SQL 调优", "topproc": "Top 存储过程", "topsql": "Top SQL",
         "vacuum": "vacuum", "waitevent": "等待事件", "wdr": "WDR 报告"}

L = ["# 诊断脚本白名单清单（%d 条）" % N, "",
     "本清单与 `docs/delivery/scripts.sql` 一一对应：**导什么就列什么**——每条的 SQL 正文直接取自 `scripts.sql`，"
     "用途与参数说明取自 `agent/scripts/registry/`，生成时逐条核对两边正文一致。",
     "全部是只读查询，只访问系统视图与系统表，不读业务数据、不含 DML / DDL。",
     "**执行库**：标「业务库」的脚本 `database_type = appbusiness`，参数最前面是 `dbName`，中间件按调用时传入的业务库名切库执行；"
     "标「postgres」的在 postgres 库执行（SQL 执行历史、WDR 快照、实例级视图都在这里）。", "",
     "| 文件 | 用途 |", "|---|---|",
     "| [`scripts.sql`](scripts.sql) | 全量 %d 条，**第一次部署**导这一份 |" % N,
     ] + ["| [`%s`](%s) | %s |" % (f, f, d) for f, d in DELTAS] + ["",
     "## 按技能分组的目录", "", "| 技能 | 条数 | 脚本 |", "|---|---|---|"]
for g, items in groups.items():
    L.append("| %s（`%s`） | %d | %s |" % (SKILL.get(g, g), g, len(items), "、".join("`%s`" % n for n, _, _, _ in items)))
L.append("")
idx = 0
for g, items in groups.items():
    L += ["## %s（`%s`，%d 条）" % (SKILL.get(g, g), g, len(items)), ""]
    for n, c, params, dbtype in items:
        idx += 1
        d = reg[n]
        L += ["### %d. `%s`" % (idx, n), "", "**用途**：%s" % " ".join(str(d.get("description", "")).split()), "",
              "**执行库**：%s" % ("业务库（appbusiness，调用时带 dbName）" if dbtype == "appbusiness" else "postgres"), ""]
        if n in ONLY_A:
            L += ["**适用**：%s" % ONLY_A[n], ""]
        if params:
            descs = {p["key"]: " ".join(str(p.get("description", "")).split()) for p in (d.get("params") or [])}
            descs["dbName"] = "业务库名（路由参数：中间件据此切到该库执行，不进 SQL）"
            L += ["| 参数 | 类型 | 说明 |", "|---|---|---|"]
            for p in params:
                L.append("| `%s` | %s | %s |" % (p["key"], p["type"], descs.get(p["key"], "").replace("|", "\\|")))
            L.append("")
        else:
            L += ["**参数**：无", ""]
        L += ["```sql", c.rstrip("\n"), "```", ""]
out = ROOT / "docs/delivery/白名单脚本清单.md"
out.write_text("\n".join(L), encoding="utf-8")
print("写出", out, "共", idx, "条,", len(groups), "组,", out.stat().st_size // 1024, "KB")
