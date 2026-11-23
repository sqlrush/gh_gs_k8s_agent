"""造数据(一次性):跑 sql/schema.sql、建分区表、按设计收集统计信息。

**统计信息是分两类处理的,这是设计不是疏漏:**
  · 大部分表 prepare 完就 ANALYZE —— 不然连「正常的计划」都没有,慢 SQL 场景
    分不清是设计的坏计划还是单纯没统计信息;
  · `import_stage` 永不 ANALYZE(V7 / S5 要的就是它的统计信息停在 0 行)。

**og5 上实测:** 100 万行约 1.6 秒、300 万行约 3.3 秒,整个 prepare 一分钟内完事。
所以 prepare 不做增量、不做断点续传 —— 每次整个重建,比维护「上次到哪了」可靠。
"""
from __future__ import annotations

import time
from typing import Dict, List

from . import config as C
from .db import MAINT_GUARDS, Conn, LabError, assert_expected_instance

# 这些表 prepare 完就收集统计信息。import_stage 不在列 —— 见模块 docstring。
ANALYZE_TABLES = tuple(t.name for t in C.TABLES if t.name != "import_stage")


def _schema_sql() -> str:
    path = C.SQL_DIR / "schema.sql"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LabError("读不到 %s:%s" % (path, exc)) from exc


def partition_ddl(parts: int = C.PARTITIONS, rows: int = 100_000) -> List[str]:
    """L4 的千分区表。

    分区 DDL 不写在 schema.sql 里:1000 个分区手写一遍没人看得下去,而且分区数要能改
    (机器小就调小)。这里生成,`parts` 只接受正整数 —— 它直接拼进 DDL。
    """
    if not isinstance(parts, int) or parts < 2 or parts > 10_000:
        raise LabError("分区数要在 2~10000 之间,给的是 %r" % (parts,))
    if not isinstance(rows, int) or rows < 0:
        raise LabError("行数不能是 %r" % (rows,))
    # 第 i 个分区收 bucket < i,所以 bucket 取 0 … parts-1(不是 1 … parts ——
    # 那样最大的那个值会落在所有分区之外,插入时报「没有匹配的分区」)。
    body = ",\n  ".join("PARTITION p%d VALUES LESS THAN (%d)" % (i, i) for i in range(1, parts + 1))
    return [
        "DROP TABLE IF EXISTS %s.part_log CASCADE" % C.SCHEMA,
        "CREATE TABLE %s.part_log (id bigint, bucket int, payload varchar(100))\n"
        "  PARTITION BY RANGE (bucket) (\n  %s\n)" % (C.SCHEMA, body),
        "INSERT INTO %s.part_log SELECT g, g %% %d, 'p-' || g FROM generate_series(1, %d) g"
        % (C.SCHEMA, parts, rows),
        "CREATE INDEX idx_part_log_bucket ON %s.part_log (bucket) LOCAL" % C.SCHEMA,
    ]


def import_batch(c: Conn, rows: int = C.IMPORT_ROWS) -> None:
    """V7:往 import_stage 导一批数据,**导完不做 ANALYZE**。

    prepare 与 V7 场景共用这一个函数 —— 两处各写一遍的话,很容易有一处顺手加上
    ANALYZE,那 S5 的「估错一个数量级」就没了,而且看不出是哪一边改坏的。
    """
    if not isinstance(rows, int) or rows <= 0 or rows > 5_000_000:
        raise LabError("导入行数不合法:%r" % (rows,))
    # **重建,不是 TRUNCATE:** TRUNCATE 清不掉 pg_statistic 里的列统计。2026-10-24 第二轮,
    # 上午那次自动 ANALYZE 留下的列统计(cust_id 的高频值)一直在,导入的又是同分布的数据,
    # S5 的估算因此是准的(估 11304、实际约 11026),根本没有「统计过期」。
    for sql in import_stage_ddl():
        c.exec(sql)
    # cust_id 故意倾斜:三分之一的行都挂在 BIG_CUSTOMER_ID 上。S5 要的「估少 60 倍」
    # 来自这个倾斜 —— 没有统计信息时优化器按默认选择率(约 0.5%)猜,实际有三分之一。
    c.exec("INSERT INTO %s.import_stage "
           "SELECT g, (g %% 1000000) + 1, "
           "       CASE WHEN g %% 3 = 0 THEN %d ELSE (g %% 200000) + 1 END, "
           "       ((g %% 5000) + 1)::numeric(12,2), now() "
           "  FROM generate_series(1, %d) g" % (C.SCHEMA, C.BIG_CUSTOMER_ID, rows))


# ---------------------------------------------------------------- 运行期对象(不在 schema.sql 里)
#
# **为什么单独建、而且要能补建:** 这几样是 2026-10-24 那轮之后为了把场景稳定造出来才加的,
# og5 上的数据早就备好了,不该为了它们把 900 MB 数据整个重造一遍。所以 run 每次开跑前都
# 「有就不动、没有就补」(rebuild=False);prepare 则整个重建(rebuild=True)。
# ustore 不可用的实例上退回普通表,并如实写明 —— 那时 L2 / L3 多半争不起来。

def slow_key_fn_ddl() -> str:
    """K5 / V6 的慢表达式:每行做 n 轮 md5。IMMUTABLE,才能进索引表达式。"""
    return ("CREATE OR REPLACE FUNCTION %s.fl_slow_key(t text, n int) RETURNS text\n"
            "  LANGUAGE plpgsql IMMUTABLE AS $$\n"
            "DECLARE r text := coalesce(t, '');\n"
            "BEGIN\n"
            "  FOR i IN 1..n LOOP r := md5(r); END LOOP;\n"
            "  RETURN r;\n"
            "END $$" % C.SCHEMA)


def slow_key_expr(column: str, iters: int) -> str:
    """索引里的慢表达式。iters 直接拼进 DDL,只收正整数。"""
    if not isinstance(iters, int) or iters <= 0 or iters > 100_000:
        raise LabError("慢表达式的轮数要是 1~100000 的整数,给的是 %r" % (iters,))
    if not column.isidentifier():
        raise LabError("列名不合法:%r" % (column,))
    return "%s.fl_slow_key(%s, %d)" % (C.SCHEMA, column, iters)


def _with_storage(ustore: bool) -> str:
    return " WITH (storage_type = ustore)" if ustore else ""


def runtime_ddl(ustore: bool) -> Dict[str, List[str]]:
    """每样运行期对象的建法(纯函数,单测钉住)。键是对象名,值是依次执行的语句。"""
    s = C.SCHEMA
    return {
        "fl_slow_key": [slow_key_fn_ddl()],
        # L2:100 行挤在一个页上。ustore 是原地更新,回收水位被 V2/V3/K10 卡住时页也不会胀开 ——
        # 普通表(astore)每次更新都留一个旧版本,水位一卡,100 行很快散到几百个页上,
        # 「热点页」就没了(2026-10-24 那轮 hotpage 胀到 116076 个死元组、4 MB)。
        "hotpage": ["DROP TABLE IF EXISTS %s.hotpage CASCADE" % s,
                    "CREATE TABLE %s.hotpage (id int PRIMARY KEY, cnt int NOT NULL DEFAULT 0)%s"
                    % (s, _with_storage(ustore)),
                    "INSERT INTO %s.hotpage SELECT g, 0 FROM generate_series(1, 100) g" % s],
        # L3:每条会话各改自己那一行(1 / 401 / 801,分在不同页上)—— 争的是事务号与提交,不是页。
        "xact_tick": ["DROP TABLE IF EXISTS %s.xact_tick CASCADE" % s,
                      "CREATE TABLE %s.xact_tick (id int PRIMARY KEY, n bigint NOT NULL DEFAULT 0)%s"
                      % (s, _with_storage(ustore)),
                      "INSERT INTO %s.xact_tick SELECT g, 0 FROM generate_series(1, 1000) g" % s],
        # W1:大字段批量写入的落点。正文是几 KB 的随机文本,压缩不了,每行都进 TOAST。cleanup 清空。
        "doc_blob": ["DROP TABLE IF EXISTS %s.doc_blob CASCADE" % s,
                     "CREATE TABLE %s.doc_blob (id bigserial PRIMARY KEY, who varchar(32), body text, "
                     "wrote_at timestamp DEFAULT now())" % s],
        # L5:每个子事务写一行,cleanup 清空。
        "subx_log": ["DROP TABLE IF EXISTS %s.subx_log CASCADE" % s,
                     "CREATE TABLE %s.subx_log (id bigint, who varchar(16), at timestamp DEFAULT now())" % s],
        # D3 / D5 自己的外键子表。原来它们用 acct_child,而 K4 每 3 分钟有 2 分钟用 LOCK TABLE 锁着 acct_child:
        # 2026-10-24 第二轮 D5 三轮都卡在 K4 的表锁上(死锁 +0),D3 也只撞出 2 次。
        "dl_child": ["DROP TABLE IF EXISTS %s.dl_child CASCADE" % s,
                     "CREATE TABLE %s.dl_child (id int PRIMARY KEY, acct_id int NOT NULL REFERENCES %s.acct (id), "
                     "amount numeric(18, 2) NOT NULL DEFAULT 1)" % (s, s),
                     "INSERT INTO %s.dl_child SELECT g, g, 1 FROM generate_series(1, %d) g" % (s, C.DL_CHILD_ROWS)],
        # V6:unlogged —— VACUUM FULL 整表重写不产生 WAL(V4 的复制槽正钉着 WAL,不能再给它添几百 MB)。
        # 慢表达式索引让每次重建都要两分钟上下,VACUUM FULL 因此能持续堵住整个时段。
        "maint_hist": ["DROP TABLE IF EXISTS %s.maint_hist CASCADE" % s,
                       "CREATE UNLOGGED TABLE %s.maint_hist AS SELECT g AS id, 0 AS cnt, md5(g::text) AS pad "
                       "  FROM generate_series(1, %d) g" % (s, C.MAINT_ROWS),
                       "ALTER TABLE %s.maint_hist ADD PRIMARY KEY (id)" % s,
                       "CREATE INDEX idx_maint_hist_slow ON %s.maint_hist ((%s))"
                       % (s, slow_key_expr("pad", C.V6_SLOW_ITERS))],
    }


def import_stage_ddl() -> List[str]:
    """import_stage 的建法(与 schema.sql 一致):一张从没被 ANALYZE 过的表,列统计也是空的。"""
    return ["DROP TABLE IF EXISTS %s.import_stage CASCADE" % C.SCHEMA,
            "CREATE TABLE %s.import_stage (id bigint, order_id bigint, cust_id bigint, "
            "amount numeric(12, 2), batch_at timestamp) WITH (autovacuum_enabled = false, "
            "autovacuum_analyze_threshold = %d, autovacuum_analyze_scale_factor = %d)"
            % (C.SCHEMA, C.IMPORT_ANALYZE_THRESHOLD, C.IMPORT_ANALYZE_SCALE)]


def import_stage_options_sql() -> str:
    """V7:import_stage 的表级参数 —— 关 autovacuum,并把 analyze 触发线拉到永远够不着
    (光关 autovacuum 挡不住自动 ANALYZE,见 config.IMPORT_ANALYZE_THRESHOLD)。"""
    return ("ALTER TABLE %s.import_stage SET (autovacuum_enabled = false, "
            "autovacuum_analyze_threshold = %d, autovacuum_analyze_scale_factor = %d)"
            % (C.SCHEMA, C.IMPORT_ANALYZE_THRESHOLD, C.IMPORT_ANALYZE_SCALE))


def _has_table(c: Conn, name: str) -> bool:
    return bool(c.one("SELECT count(*) FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace "
                      " WHERE n.nspname = %s AND t.relname = %s AND t.relkind = 'r'", (C.SCHEMA, name)))


def _is_ustore(c: Conn, name: str) -> bool:
    opts = c.one("SELECT array_to_string(t.reloptions, ',') FROM pg_class t "
                 "  JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = %s AND t.relname = %s",
                 (C.SCHEMA, name))
    return "storage_type=ustore" in str(opts or "").lower().replace(" ", "")


def ustore_available(c: Conn) -> bool:
    return str(c.one("SELECT setting FROM pg_settings WHERE name = 'enable_ustore'") or "").lower() == "on"


def _needs_build(c: Conn, name: str, ustore: bool) -> bool:
    """这样东西要不要(重)建。"""
    if name == "fl_slow_key":
        return True                                    # CREATE OR REPLACE,便宜,每次都做
    if not _has_table(c, name):
        return True
    if name in ("hotpage", "xact_tick") and ustore and not _is_ustore(c, name):
        return True                                    # 老的普通表,升级成 ustore
    if name == "maint_hist":
        rows = c.one("SELECT count(*) FROM %s.maint_hist" % C.SCHEMA) or 0
        idx = c.one("SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid "
                    " JOIN pg_namespace n ON n.oid = x.relnamespace "
                    " WHERE n.nspname = %s AND x.relname = 'idx_maint_hist_slow'", (C.SCHEMA,))
        # unlogged 表在实例崩溃恢复后会被清空;轮数改过的话索引也要按新轮数重建
        return int(rows) < C.MAINT_ROWS or ", %d)" % C.V6_SLOW_ITERS not in str(idx or "")
    return False


def ensure_runtime_objects(c: Conn, rebuild: bool = False) -> List[str]:
    """补建(或重建)运行期对象,并把 import_stage 的表级参数设到位。返回给人看的几行说明。"""
    c.set("query_dop", "1")          # 慢索引按单核建:耗时与 V6 的 VACUUM FULL 重建同口径,也不多占核
    ustore = ustore_available(c)
    out: List[str] = []
    for name, stmts in runtime_ddl(ustore).items():
        if not rebuild and not _needs_build(c, name, ustore):
            continue
        t0 = time.time()
        try:
            for sql in stmts:
                c.exec(sql)
        except Exception as exc:                     # noqa: BLE001
            if not ustore or "ustore" not in " ".join(stmts):
                raise
            c.rollback()                             # 这个实例建不了 ustore 表:退回普通表,写明
            for sql in runtime_ddl(False)[name]:
                c.exec(sql)
            out.append("%s:建 ustore 表失败(%s),退回普通表 —— 回收水位被卡住时页会胀开,轻量锁可能争不起来"
                       % (name, str(exc).splitlines()[0][:80]))
            continue
        if name != "fl_slow_key":
            # 建完就收集统计:不然这几张表一直「从没 ANALYZE 过」,health 的 STALE_STATS 会把它们
            # 当成故障报出来(2026-10-24 第二轮 maint_hist 就这样成了噪声)。import_stage 不在这里。
            c.exec("ANALYZE %s.%s" % (C.SCHEMA, name))
            kind = "ustore" if ustore and name in ("hotpage", "xact_tick") else "普通表"
            out.append("已建 %s(%s,%.0f 秒)" % (name, kind, time.time() - t0))
    for name in C.RUNTIME_TABLES:                 # 早先版本建好、却从没 ANALYZE 过的,补一次
        if _has_table(c, name) and not c.one(
                "SELECT count(*) FROM pg_stats WHERE schemaname = %s AND tablename = %s", (C.SCHEMA, name)):
            c.exec("ANALYZE %s.%s" % (C.SCHEMA, name))
    c.exec(import_stage_options_sql())
    return out


def run(verbose: bool = True) -> Dict[str, object]:
    """建全部库对象并造数。返回每一步的耗时,方便看是哪一步慢了。"""
    c = Conn("prepare", guards=MAINT_GUARDS)
    steps: Dict[str, object] = {}
    try:
        fp = assert_expected_instance(c)
        steps["实例"] = fp["data_dir"]
        _timed(steps, "建表造数", verbose, lambda: c.exec(_schema_sql()))
        _timed(steps, "千分区表", verbose, lambda: [c.exec(s) for s in partition_ddl()])
        _timed(steps, "运行期对象(慢表达式函数、V6/L2/L3/L5 的表)", verbose,
               lambda: steps.setdefault("运行期对象", ensure_runtime_objects(c, rebuild=True)))
        _timed(steps, "收集统计信息", verbose,
               lambda: [c.exec("ANALYZE %s.%s" % (C.SCHEMA, t)) for t in ANALYZE_TABLES])
        # 放在 ANALYZE **之后**:这张表要的就是没有统计信息(见函数 docstring)
        _timed(steps, "导入 import_stage(不收集统计信息)", verbose, lambda: import_batch(c))
        steps["占用空间"] = size_report(c)
        steps["行数核对"] = _verify_rows(c)
    finally:
        c.close()
    return steps


def _timed(steps: Dict[str, object], label: str, verbose: bool, fn) -> None:
    t0 = time.time()
    if verbose:
        print("  %s …" % label, flush=True)
    fn()
    steps[label] = "%.1fs" % (time.time() - t0)


def size_report(c: Conn) -> str:
    total = c.one(
        "SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'",
        (C.SCHEMA,))
    return str(total or "?")


def _verify_rows(c: Conn) -> str:
    """造完核对行数。

    **不核对的话,「场景没造出现象」会被误当成场景逻辑的问题** —— 实际可能只是
    造数那一步某条 SQL 失败了而已(整个 schema.sql 是一次执行的,中间失败只会
    留下半套表)。这里只核对量级,不追求精确。
    """
    bad: List[str] = []
    for t in C.TABLES:
        if t.rows == 0:
            continue
        got = c.one("SELECT count(*) FROM %s.%s" % (C.SCHEMA, t.name)) or 0
        if abs(int(got) - t.rows) > max(10, t.rows * 0.02):
            bad.append("%s 实际 %s 行、期望约 %s 行" % (t.name, got, t.rows))
    return "全部相符" if not bad else "对不上:" + ";".join(bad)
