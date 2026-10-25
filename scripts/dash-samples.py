#!/usr/bin/env python3
"""生成大盘的样例报告目录 —— **用 Python 侧真实的报告类与 reports.archive() 生成**,不手写 JSON。

  scripts/dash-samples.py <输出目录>

输出目录的布局就是用户 Pod 里 /nas/me/reports 的布局(4097 端出来的那份):
  health/og5/{<ts>.json,latest.json,index.json}  health/targets.json
  topsql/og5/…  wdr/og5/…  sqltune/og5/1a9f.json  kb/health.json  kb/queries.jsonl  whoami.json

两个用途:
  · 前端开发的样例数据:把输出目录当成 /reports/ 端出来(任何静态服务器都行),页面不连真集群也能开发与验收;
  · agent/tests/test_frontend_contract_units.py 拿同一批构造函数核对 docs/api/reports-contract.json。
样例形状一变,两边一起变 —— 这就是不手写 JSON 的原因。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(_ROOT))

from common import reports  # noqa: E402
from common.finding import Finding, Severity  # noqa: E402

CONN = "og5"
TS = ["20260920T090000Z", "20260921T090700Z", "20260922T140300Z"]


def _load(scripts: str, *purge: str):
    for m in purge:
        sys.modules.pop(m, None)
    sys.path.insert(0, str(_ROOT / "skills" / scripts / "scripts"))


def health_samples():
    _load("gaussdb-health", "model", "thresholds", "util", "collectors", "report", "health", "aggregate", "render")
    import model, report  # noqa: E402
    # 维度名、表头、行形状都照 gaussdb-health/scripts/collectors.py —— 第一版样例是按设计稿编的,
    # 页面对着它写,真数据上维度中文名、表格全都对不上(2026-09-23 真集群截图发现)。
    M = model
    dims = [
        M.DimResult(dimension=M.DIM_OVERVIEW, headline="命中率 99.1%、连接 61/1000、未在恢复、最老事务 无",
                    headers=["cache_hit%", "connections", "max_conn", "in_recovery", "最老事务"], rows=[["99.1", "61", "1000", "false", ""]]),
        M.DimResult(dimension=M.DIM_SLOWSQL, headline="Top1 avg 8412ms ×214(共3条超阈值)",
                    headers=["sql_id", "calls", "avg_ms", "total_s", "cpu_s", "query"],
                    rows=[["1a9f", "214", "8412.00", "1800.17", "12.40", "SELECT r.region, s.store, sum(f.amount) FROM fact_sales f JOIN customers c ON c.id=f.cust_id GROUP BY 1,2"],
                          ["77e0", "1880", "2105.00", "3957.40", "2.10", "SELECT category, sum(amount) FROM fact_sales WHERE sale_date >= $1 GROUP BY category"]]),
        M.DimResult(dimension=M.DIM_XACT, headline="1 个空闲中事务已持续 6 分钟",
                    headers=["pid", "user", "state", "时长(s)", "query"],
                    rows=[["140233", "batch", "idle in transaction", "372", "UPDATE accounts SET balance = balance - $1 WHERE id = $2"]]),
        M.DimResult(dimension=M.DIM_CONN, headline="共 61:active 14、idle 47", headers=["state", "会话数"], rows=[["active", "14"], ["idle", "47"]]),
        M.DimResult(dimension=M.DIM_LOGS, headline="checkpoint req 占比 0%、归档 off", headers=["指标", "值"],
                    rows=[["checkpoint timed/req", "17695/41"], ["archive_mode", "off"]]),
        M.DimResult(dimension=M.DIM_REPL, headline="1 个备机 · 延迟 0.2 s",
                    headers=["standby", "client", "state", "sync", "replay_lag"], rows=[["dn_6002", "10.0.0.12", "streaming", "sync", "0.2 s"]]),
        M.DimResult(dimension=M.DIM_SCHEMA, headline="窗口内未使用的索引 2、无主键大表 1(观测窗口 自 2026-09-06 起 17.0 天)",
                    headers=["项", "对象", "值"],
                    rows=[["观测窗口", "pg_stat_database.stats_reset", "自 2026-09-06 11:56:28 起 17.0 天"],
                          ["普通索引", "public.idx_orders_tmp", "795.0M 扫描 0 次"], ["无主键大表", "public.fact_sales", "6.1 GB"]]),
        M.DimResult(dimension=M.DIM_CONCURRENCY, headline="死锁 0、回滚率 0.0%、2PC 0", headers=["指标", "值"],
                    rows=[["deadlocks", "0"], ["commit/rollback", "1558509/597 (0.0%回滚)"]]),
    ]
    findings = [
        Finding(M.DIM_SLOWSQL, "SLOW_AVG_MS", Severity.WARN, "avg_ms", "8,412", "> 1,000", "sql_id 1a9f · 214 次", sql_id="1a9f"),
        Finding(M.DIM_SCHEMA, "UNUSED_INDEX", Severity.NOTICE, "idx_scan", "0", "> 10M 且 0 次", "public.idx_orders_tmp 795.0M"),
        Finding(M.DIM_XACT, "IDLE_IN_XACT", Severity.NOTICE, "idle_in_xact_min", "6", "> 5", "pid 140233 · app=batch-loader"),
        # 子技能(waitevent)的发现带自己的维度名,不属于上面 8 个维度 —— 页面要单独计数
        Finding("DB Time", "LWLOCK_EVENT", Severity.WARN, "LWLOCK_EVENT 耗时占 DB_TIME", "42.8%", ">=10.0%",
                "snapshot.snap_global_wait_events wait_class=LWLOCK_EVENT"),
    ]
    for d in dims:
        d.findings = [f for f in findings if f.dimension == d.dimension]
    subs = [types.SimpleNamespace(skill="gaussdb-lockwait", ok=True, error=""),
            types.SimpleNamespace(skill="gaussdb-waitevent", ok=True, error=""),
            types.SimpleNamespace(skill="gaussdb-vacuum", ok=False, error="超时 60 s")]
    for i, (ts, overall, fs) in enumerate(zip(TS, [Severity.NOTICE, Severity.NOTICE, Severity.WARN], [findings[2:3], findings[1:3], findings])):
        ev = model.HealthEvidence(conn=CONN, dims=dims, findings=fs, overall=overall)
        reports.archive("health", report.health_dict(ev, sub_results=subs), instance=CONN, now=ts)

    down = "请求中间件失败:Remote end closed connection without response"
    failed = [model.DimResult(dimension=x.dimension, available=False, note=down, headline="不可用:" + down) for x in dims]
    bad_subs = [types.SimpleNamespace(skill=s.skill, ok=False, error="error: " + down) for s in subs]
    ev = model.HealthEvidence(conn="og-std", dims=failed, findings=[], overall=Severity.OK)
    reports.archive("health", report.health_dict(ev, sub_results=bad_subs), instance="og-std", now=TS[2])


def topsql_samples():
    _load("gaussdb-topsql", "topsql", "render")
    import topsql  # noqa: E402
    rows = [
        topsql.StmtRow("0b1c…e7", "SELECT sessid, sum(usedsize) FROM gs_session_memory_detail GROUP BY sessid", 1728000, 26470.0, 15.3, 1728000),
        topsql.StmtRow("9d40…13", "SELECT category, sum(amount) FROM fact_sales WHERE sale_date >= $1 GROUP BY category", 41200, 7420.0, 180.1, 41200),
        topsql.StmtRow("1a9f", "SELECT r.region, s.store, sum(f.amount) FROM fact_sales f JOIN customers c ON c.id=f.cust_id JOIN stores s ON s.id=f.store_id GROUP BY 1,2", 214, 1800.0, 8412.0, 2140),
        topsql.StmtRow("4c77…a0", "SELECT sale_date, sum(amount) FROM fact_sales GROUP BY sale_date", 8800, 1410.0, 160.2, 8800),
        topsql.StmtRow("e2b9…55", "UPDATE accounts SET balance = balance - $1 WHERE id = $2", 9800000, 980.0, 0.1, 9800000),
    ]
    for by, ts in (("time", TS[1]), ("avg", TS[2])):
        payload = {"conn": CONN, "by": by, "limit": 10, "rows": [r.__dict__ for r in rows]}
        reports.archive("topsql", payload, name="%s.%s" % (ts, by), instance=CONN, now=ts)


def wdr_samples():
    _load("gaussdb-wdr", "model", "thresholds", "util", "collectors", "report", "native", "snaps",
          "interp", "recheck", "finalreport", "ansi", "wdr", "render")
    import model  # noqa: E402
    def ev_for(begin, end, dbtime, aas, hit, reads, sev):
        ev = model.Evidence(conn=CONN)
        ev.window = model.Window(begin_id=begin, end_id=end, begin_ts="2026-09-22 10:30:00", end_ts="2026-09-22 10:39:00",
                                 duration_min=9, scope="node", node="dn_6001", wdr_enabled=True)
        # 维度名、表头、行形状照 gaussdb-wdr/scripts/collectors.py:Load Profile 与 Database Stat 各一行、按列存值。
        # 第一版样例把它们编成「按行名存」,页面对着写,真数据上 8 个 KPI 全是「未采集」。
        M = model
        cpu_pct = 72.0 if sev else 86.0
        ev.dims = [
            M.DimResult(dimension=M.DIM_LOADPROFILE, headline="DB time %ss(其中 CPU %.0f%%)" % (dbtime, cpu_pct),
                        headers=["DB time(s)", "CPU time(s)", "CPU占DBtime%", "commits", "物理读(块)", "逻辑读(块)"],
                        rows=[[f"{dbtime:.2f}", f"{dbtime * cpu_pct / 100:.2f}", f"{cpu_pct:.2f}", "14688", str(reads * 540), "3200000"]]),
            M.DimResult(dimension=M.DIM_DBSTAT, headline="commit 14688 / rollback 44(回滚率 0.3%%)、命中率 %s%%" % hit,
                        headers=["commits", "rollbacks", "回滚率%", "死锁", "临时溢出", "cache_hit%"],
                        rows=[["14688", "44", "0.30", "0", "19763.20MiB" if sev else "0.00MiB", str(hit)]]),
            M.DimResult(dimension=M.DIM_TOPSQL, headline="各维度元凶 — DB time:1a9f(41%)",
                        headers=["sql_id", "calls", "elapsed_s", "cpu_s", "spill_MB", "物理读(块)", "占DB time%", "query"],
                        rows=[["1a9f", "3", "2410.00", "610.20", "19300.00", "1200000", "41.53",
                               "SELECT r.region, s.store, sum(f.amount) FROM fact_sales f JOIN customers c ON c.id=f.cust_id GROUP BY 1,2"],
                              ["4c77", "2", "1180.00", "330.00", "0.00", "610000", "20.33", "SELECT sale_date, sum(amount) FROM fact_sales GROUP BY sale_date"],
                              ["e2b9", "14800", "95.00", "40.00", "0.00", "1200", "1.64", "UPDATE accounts SET balance = balance - $1 WHERE id = $2"]]),
            M.DimResult(dimension=M.DIM_WAITS, headline="Top 等待类 IO_EVENT 占 81%",
                        headers=["等待类", "waits", "wait_s", "占比%"],
                        rows=[["IO_EVENT", "120433", "437.00", "81.00"], ["LWLOCK_EVENT", "9001", "70.00", "13.00"], ["LOCK_EVENT", "12", "32.00", "6.00"]]),
            M.DimResult(dimension=M.DIM_CHECKPOINT, headline="checkpoint timed 2 / req 0(req 占 0%)",
                        headers=["timed_ckpt", "req_ckpt", "req占比%"], rows=[["2", "0", "0.00"]]),
            M.DimResult(dimension=M.DIM_CACHE, headline="fact_sales 物理读最多", headers=["对象", "物理读(块)", "逻辑读(块)"],
                        rows=[["fact_sales", "1900000", "3200000"], ["customers", "140000", "900000"]]),
            M.DimResult(dimension=M.DIM_FILEIO, headline="物理读 Top 文件:db14725/spc1663/f39021", headers=["文件", "物理读", "物理写"],
                        rows=[["db14725/spc1663/f39021", "2040000", "31000"], ["db14725/spc1663/f39022", "410000", "900"]]),
        ]
        # sql_id → 完整 SQL。表格里的 query 列是**截断过的**显示值,全文在这里 ——
        # 大盘的 SQL 抽屉(lib/details.js:openSql)从 WDR 页点进来时就靠它,
        # 因为那条 sql_id 常常不在 Top SQL 的存档里。
        ev.sql_texts = {
            "1a9f": ("SELECT r.region, s.store, sum(f.amount) FROM fact_sales f "
                     "JOIN customers c ON c.id = f.cust_id JOIN regions r ON r.id = c.region_id "
                     "JOIN stores s ON s.id = f.store_id WHERE f.sale_date >= $1 GROUP BY 1, 2 "
                     "ORDER BY 3 DESC"),
            "4c77": "SELECT sale_date, sum(amount) FROM fact_sales GROUP BY sale_date ORDER BY 1",
            "e2b9": "UPDATE accounts SET balance = balance - $1 WHERE id = $2",
        }
        ev.findings = [Finding(M.DIM_WAITS, "WDR_WAIT_CLASS_SKEW", Severity.WARN, "等待类倾斜 IO_EVENT", "81%", ">60%", "snap_global_wait_events 等待类聚合"),
                       Finding(M.DIM_TOPSQL, "WDR_SQL_DBTIME_SHARE", Severity.CRITICAL, "单条 SQL 占 DB time", "41.53%", ">30%",
                               "snap_summary_statement:sqlid 1a9f elapsed 2410s ×3")][:sev]
        ev.overall = Severity.CRITICAL if sev else Severity.OK
        ev.native = model.NativeInfo(generated=True, bytes=1_800_000, saved_path="/nas/me/reports/wdr/og5/%s.native.html" % TS[2])
        return ev
    reports.archive("wdr", ev_for(1914, 1915, 2972, 0.97, 96.4, 406, 0).to_dict(), instance=CONN, now=TS[1])
    reports.archive("wdr", ev_for(1915, 1916, 5803, 10.7, 93.6, 3771, 2).to_dict(), instance=CONN, now=TS[2])
    # 原生报告文件本身也写一份(占位),预览站上「下载 HTML →」才下得到
    native = reports.reports_dir() / "wdr" / CONN / ("%s.native.html" % TS[2])
    native.write_text("<html><body><h1>WDR 样例</h1>" + "<p>占位</p>" * 300 + "</body></html>", encoding="utf-8")


def sqltune_samples():
    payload = {"conn": CONN, "sql_id": "1a9f", "source": "statement_history", "schema": "public",
               "original_sql": "SELECT r.region, s.store, sum(f.amount) FROM fact_sales f JOIN customers c ON c.id=f.cust_id GROUP BY 1,2",
               "substitution": {"sql": "", "placeholders": 0, "substitutions": []},
               "evidence": {"version": "openGauss 5.0", "analyzed": True, "plan": "Seq Scan on fact_sales (cost=0..612000)",
                            # 形状 = skills/gaussdb-sqltune/scripts/evidence.py 的 Finding.__dict__
                            "findings": [{"kind": "seq_scan_big_table", "severity": "warn",
                                          "detail": "fact_sales 全表扫描 6.1 GB",
                                          "advice": "建 (store_id, sale_date) 复合索引;先按 store 聚合再 JOIN regions"}],
                            "tables": [], "indexes": [], "columns": [], "gucs": []}}
    reports.archive("sqltune", payload, name="1a9f", instance=CONN, now=TS[2])
    # 监控类 SQL:调优技能按策略跳过时存的那份(sqltune.skip_payload 的形状),Top SQL 页据此不再挂「调优 →」
    reports.archive("sqltune", {"sql_id": "0b1c…e7", "conn": CONN, "skipped": "system", "system_objects": ["gs_session_memory_detail"]},
                    name="0b1c…e7", instance=CONN, now=TS[2])


def kb_samples(out: pathlib.Path):
    sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-kb" / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("kb_cli_for_samples", _ROOT / "skills" / "gaussdb-kb" / "scripts" / "kb.py")
    kb = importlib.util.module_from_spec(spec); sys.modules["kb_cli_for_samples"] = kb; spec.loader.exec_module(kb)
    from common.kb import query as kbquery
    kbdir = out / "_kbdir"; (kbdir / "index").mkdir(parents=True, exist_ok=True)
    (kbdir / "index" / "misses.log").write_text("2026-09-20\tNOPK_BIGTABLE\tq\n2026-09-21\tNOPK_BIGTABLE\tq\n2026-09-21\tIDLE_IN_XACT\tq\n", encoding="utf-8")
    st = kbquery.KbStatus(attached=True, version="3", counts={"docs.case": 286, "docs.rule": 412, "docs.raw": 341},
                          vector="sqlite-vec(覆盖 93%)", graph="图文件", mode="向量库+图文件")
    d = kb.health_dict(kbdir, st, ["cases/bad.md: 缺 frontmatter"], True)
    reports.archive("kb", d, name="health")
    for row in [{"at": "2026-09-22T14:05:00Z", "q": "慢 SQL 平均耗时超阈值 · 全表扫描", "key": "SLOW_AVG_MS", "hits_cases": 3, "hits_rules": 2, "how": "semantic", "elapsed_ms": 42},
                {"at": "2026-09-22T11:40:00Z", "q": "无主键大表怎么处理", "key": "NOPK_BIGTABLE", "hits_cases": 0, "hits_rules": 1, "how": "keyword", "elapsed_ms": 30},
                {"at": "2026-09-21T16:22:00Z", "q": "idle in transaction 6 分钟", "key": "IDLE_IN_XACT", "hits_cases": 0, "hits_rules": 0, "how": "keyword", "elapsed_ms": 25}]:
        reports.append_jsonl("kb", "queries", row)
    import shutil; shutil.rmtree(kbdir, ignore_errors=True)


_CASES = [
    ("S2-20250908-CBST-灌数未ANALYZE计划跳变致CPU打满", "灌数未 ANALYZE 计划跳变致 CPU 打满", "CBST", "2025-09-08",
     "灌数后统计信息停在旧值,优化器选错计划", ["GS-OPS-001"],
     {"现场": "夜间灌数后联机 CPU 占 DB time 84%,主机 CPU 打满", "判断": "灌数后未 ANALYZE,统计信息停在灌数前",
      "处置": "作业代码里补显式 ANALYZE,计划恢复"},
     [("DBTIME_CPU_HEAVY", "灌数后统计信息失真", "作业里显式 ANALYZE 相关表")]),
    ("S2-20250812-CBMS-两过程更新顺序相反致死锁频发", "两过程更新顺序相反致死锁频发", "CBMS", "2025-08-12",
     "两个存储过程以相反顺序更新客户表与账户表", ["GS-DML-001"],
     {"现场": "死锁每天增加十几次", "判断": "两过程更新顺序相反", "处置": "统一为 客户表 → 额度表 → 账户表"},
     [("死锁频发,deadlocks 每天增加十几次", "两个存储过程更新顺序相反", "统一多表更新顺序")]),
    ("S1-20250314-CBST-批量大事务致WALWriteLock等待冲高TP", "批量大事务致 WALWriteLock 等待冲高 TPS 跌六成", "CBST", "2025-03-14",
     "批量单事务 20 万行,提交时 WAL 刷盘串行", ["GS-OPS-001", "GS-DML-001"],
     {"现场": "WALWriteLock 占 DB time 40%,联机 TPS 跌六成", "判断": "单事务 20 万行", "处置": "拆批到 5000 行/事务",
      "复发标志": "top 等待事件为 WALWriteLock 且与批量窗口重合"},
     [("WALWriteLock 等待冲高", "批量单事务过大", "拆批到 5000 行/事务")]),
]
_RULES = {
    "ops.yaml": ("# 运维操作规范(统计信息、会话处置、变更管理)",
                 [("GS-OPS-001", "error", "批量灌数之后必须在作业代码里显式 ANALYZE 相关表", "统计信息滞后会导致计划劣化")]),
    "dml.yaml": ("# 开发规范:增删改查",
                 [("GS-DML-001", "error", "账户类表更新按主键升序、每 5000 行一个事务;多表固定顺序:客户表 → 额度表 → 账户表", "避免死锁与长事务")]),
}


# 知识条目页签:六类类型化条目各一条(2026-10-24 八类知识)
_TYPED = (
    ("runbooks", "RB-LCK-001.md", "---\nid: RB-LCK-001\ntype: runbook\ntitle: 终止阻塞根会话\ntopics: [lock]\nsource: 运维手册 §4.2\n"
     "verification: 已验证\napproval: [GS-OPS-001]\napplies: {product: GaussDB 集中式, versions: '>= 503'}\n---\n"
     "## 摘要\n只终止阻塞链的根会话,不批量杀会话。\n## 执行前提\n确认根会话不是批量作业。\n## 步骤\n按根会话 pid 终止。\n## 回退步骤\n无需回退。\n"),
    ("defects", "KD-0001.md", "---\nid: KD-0001\ntype: defect\ntitle: 某版本下统计信息收集偶发卡住\ntopics: [vacuum]\nsource: 厂商通告\n"
     "affected_versions: 505.1–505.2\nfixed_in: 505.2.1\nvendor_status: 已确认\n---\n## 摘要\n受影响版本升级到修复补丁。\n## 规避办法\n分批收集。\n"),
    ("baselines", "BL-CBST-ACTIVE_SESSIONS.md", "---\nid: BL-CBST-ACTIVE_SESSIONS\ntype: baseline\ntitle: 活跃会话数基线\ntopics: [conn]\n"
     "system: CBST\nmetric: active_sessions\nunit: 个\nwindow: 5 分钟\nvalue_kind: 瞬时值\n---\n## 摘要\n日间 20–40,批量窗口 80 以内。\n## 正常表现\n日间 20–40。\n"),
    ("product", "PD-0001.md", "---\nid: PD-0001\ntype: product\ntitle: 锁等待超时参数的行为\ntopics: [lock]\nsource: 产品文档 · 锁\n"
     "product: GaussDB 集中式\nversions: '>= 503'\n---\n## 摘要\n超时后语句报错退出,事务需应用侧回滚。\n"),
    ("wiki", "WK-0001.md", "---\nid: WK-0001\ntype: wiki\ntitle: 连接池打满的排查顺序\ntopics: [conn]\nsource: 内部 Wiki · 连接\n"
     "verification: 未验证\nas_of: '2025-06'\n---\n## 摘要\n先看连接来源分布,再看空闲事务。\n"),
    ("profiles", "EP-CBST.md", "---\nid: EP-CBST\ntype: profile\ntitle: CBST 环境画像\nsystem: CBST\nproduct: GaussDB 集中式\n"
     "version: 505.2.0\naliases: [og5]\n---\n## 摘要\n核心账务,日终批量 22:00 起。\n## 批量窗口\n22:00–02:00。\n"),
)


def _docset_sample(kb: pathlib.Path) -> None:
    """一套很小的官方文档集(两页、三条),走真正的 docset.scan / apply。"""
    import yaml
    from common.kb import docset
    src = kb.parent / "_chm"
    src.mkdir(exist_ok=True)
    page = '<html><head><meta charset="utf-8"></head><body><h1>{t}</h1>{b}</body></html>'
    (src / "p_lock.html").write_text(page.format(t="锁管理", b=(
        '<div class="section"><h4 class="sectiontitle">deadlock_timeout</h4><p>参数说明：死锁检测时间。</p><p>默认值：1000。</p></div>'
        '<div class="section"><h4 class="sectiontitle">lockwait_timeout</h4><p>参数说明：单个锁的最长等待时间。</p><p>默认值：1200000。</p></div>')),
        encoding="utf-8")
    (src / "p_fault.html").write_text(page.format(t="锁等待超时处理", b="<p>" + "锁等待超时,业务报错。" * 10 + "</p>"), encoding="utf-8")
    obj = lambda n, l="": f'<LI><OBJECT type="text/sitemap"><param name="Name" value="{n}"><param name="Local" value="{l}"></OBJECT>'  # noqa: E731
    (src / "book.hhc").write_text("<UL>" + obj("使用指南") + "<UL>" + obj("配置运行参数") + "<UL>" + obj("GUC参数说明") + "<UL>"
                                  + obj("锁管理", "p_lock.html") + "</UL></UL></UL>" + obj("维护指南") + "<UL>" + obj("故障处理")
                                  + "<UL>" + obj("锁等待超时处理", "p_fault.html") + "</UL></UL></UL>", encoding="utf-8")
    staging = kb / "inbox" / "ds"
    docset.scan(src, staging)
    (staging / "plan").mkdir()
    (staging / "plan" / "plan.yaml").write_text(yaml.safe_dump({"code": "S25", "product": "GaussDB 集中式", "versions": "505.2",
                                                                "title": "样例官方文档", "doc_version": "25.1.32",
                                                                "kernel_version": "505.2.1"}, allow_unicode=True), encoding="utf-8")
    docset.apply(kb, docset.unpacked_root(staging), staging / "plan")


def kb_catalog_samples(out: pathlib.Path):
    """知识库页签的数据:造一个小知识库,用**真正的** docker/kbcatalog.build 生成 _kb/catalog.json。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("kbcatalog_for_samples", _ROOT.parent / "docker" / "kbcatalog.py")
    kbcatalog = importlib.util.module_from_spec(spec); spec.loader.exec_module(kbcatalog)
    kb = out / "_kbsrc"
    for sub in ("cases", "rules", "graph"):
        (kb / sub).mkdir(parents=True, exist_ok=True)
    edges, canon = [], {"symptom:dbtime_cpu_heavy": ["CPU 占 DB time 84% 以上,主机 CPU 打满", "DBTIME_CPU_HEAVY"]}
    for cid, title, system, day, factor, rules, secs, chain in _CASES:
        body = "\n".join("## %s\n%s" % (k, v) for k, v in secs.items())
        (kb / "cases" / (cid + ".md")).write_text(
            "---\nid: %s\ntitle: %s\nsystem: %s\noccurred_at: '%s'\nconclusion: 已确认\nsource: 工单导出-2025.csv#row=%d\n"
            "severity: %s\nprimary_factor: %s\nrules: [%s]\n---\n%s\n" % (cid, title, system, day, len(edges) + 2, cid[:2], factor,
                                                                           ", ".join(rules), body), encoding="utf-8")
        sym, root_cause, action = chain[0]
        for src, rel, dst, sec in (({"kind": "case", "name": title, "canonical": "case:" + cid}, "exhibits", {"kind": "symptom", "name": sym}, "现场"),
                                   ({"kind": "symptom", "name": sym}, "caused_by", {"kind": "rootcause", "name": root_cause}, "判断"),
                                   ({"kind": "rootcause", "name": root_cause}, "handled_by", {"kind": "action", "name": action}, "处置")):
            edges.append({"src": src, "rel": rel, "dst": dst, "confidence": 1.0, "status": "accepted",
                          "source": "cases/%s.md#%s" % (cid, sec), "case": cid})
    import yaml
    (kb / "graph" / "工单导出-2025.yaml").write_text(yaml.safe_dump(edges, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (kb / "graph" / "canonical.yaml").write_text(yaml.safe_dump(canon, allow_unicode=True), encoding="utf-8")
    for name, (title, items) in _RULES.items():
        (kb / "rules" / name).write_text(title + "\n" + yaml.safe_dump(
            [{"id": i, "severity": s, "check": "advisory", "rule": r, "rationale": w, "keywords": ["样例"], "source": "运维规范 §5"}
             for i, s, r, w in items], allow_unicode=True, sort_keys=False), encoding="utf-8")
    for sub, name, text in _TYPED:
        (kb / sub).mkdir(parents=True, exist_ok=True)
        (kb / sub / name).write_text(text, encoding="utf-8")
    _docset_sample(kb)
    cat = kbcatalog.build(kb)
    cat["built_at"] = "2026-09-22 22:03:00"
    (out / "_kb").mkdir(exist_ok=True)
    (out / "_kb" / "catalog.json").write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")
    # 「目录」页签(渐进式披露)的四条路:用**真正的** docker/kboutline 生成
    spec = importlib.util.spec_from_file_location("kboutline_for_samples", _ROOT.parent / "docker" / "kboutline.py")
    kboutline = importlib.util.module_from_spec(spec); spec.loader.exec_module(kboutline)
    ol = kboutline.Outline(kb)
    outline = ol.outline()
    (out / "_kb" / "outline.json").write_text(json.dumps(outline, ensure_ascii=False, indent=1), encoding="utf-8")
    page = ol.list(docset="S25", topic="lock", limit=20)
    (out / "_kb" / "list.json").write_text(json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "_kb" / "item").mkdir(exist_ok=True)
    (out / "_kb" / "evidence").mkdir(exist_ok=True)
    for item_id in (page["items"][0]["id"], outline["topics"][0]["entries"][0]["id"]):
        (out / "_kb" / "item" / f"{item_id}.json").write_text(json.dumps(ol.item(item_id), ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "_kb" / "evidence" / f"{item_id}.json").write_text(json.dumps(ol.evidence(item_id), ensure_ascii=False, indent=1), encoding="utf-8")
    import shutil; shutil.rmtree(kb, ignore_errors=True); shutil.rmtree(kb.parent / "_chm", ignore_errors=True)


def main(argv=None) -> int:
    a = argv if argv is not None else sys.argv[1:]
    if len(a) != 1:
        print(__doc__, file=sys.stderr); return 2
    out = pathlib.Path(a[0]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    os.environ["GSDB_REPORTS_DIR"] = str(out)
    health_samples(); topsql_samples(); wdr_samples(); sqltune_samples(); kb_samples(out); kb_catalog_samples(out)
    (out / "whoami.json").write_text(json.dumps({"user_id": "u1234"}), encoding="utf-8")
    print("样例报告已生成:%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
