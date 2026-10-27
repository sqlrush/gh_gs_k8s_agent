-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-10-24）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 123 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 123 条）。
--
--   新增   0 条 INSERT
--   更新   12 条 UPDATE
--
-- 全部脚本均为**只读查询**，只访问系统视图与系统表，不读业务数据、
-- 不含 DML/DDL、不修改任何对象。
--
-- 导入前请确认两处与贵方规范的对齐：
--   1. `create_user` / `last_modify_user` 当前填 '999999999'，与上一版保持一致；
--      若贵方要求真实工号，请统一替换。
--   2. id 用 `grmp.script_config_seq.nextval`，不硬编码，避免与现有记录冲突。
--
-- =====================================================================
-- 一、新增 0 条
-- =====================================================================

-- =====================================================================
-- 二、更新 12 条已有脚本的 SQL 正文
-- =====================================================================

-- [health.conn_concentration]
--   **修正漏报**：活跃连接集中度认客户端会话的条件改法同 health.long_xact，其余不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT COALESCE(query,'''') q, count(*) c, sum(count(*)) OVER () AS total
FROM pg_stat_activity
WHERE state=''active'' AND COALESCE(query,'''')<>'''' AND client_port IS NOT NULL
GROUP BY query ORDER BY c DESC LIMIT 1;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.conn_concentration';

-- [health.conn_states]
--   **修正计数**：只数 sessionid <> 0 的会话。开了线程池时有一批空闲工作线程的条目（无客户端、不持锁）， 原 SQL 把它们算进总数与 idle（实测 61 个真实会话报成 65）。与 health.thread_pool 同一口径。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT COALESCE(state,''<null>'') AS state, count(*) AS cnt
FROM pg_stat_activity
WHERE sessionid <> 0
GROUP BY state
ORDER BY cnt DESC;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.conn_states';

-- [health.long_xact]
--   **修正漏报**：「客户端会话」的认法由 connection_info 非空改为 client_port 非空。connection_info 只有 openGauss 自带的 libpq 会填，psycopg2 这类客户端是空串，原条件把它们的长事务、空闲事务整类漏掉； 内核后台线程的 client_port 都是空，照样排除。另排除执行本查询的会话自身，多返回一列 waiting （是否在等锁，供「执行中的长 SQL」区分等锁的语句）。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT pid,
       COALESCE(usename,'''') AS usename,
       state,
       waiting,
       EXTRACT(EPOCH FROM (now()-xact_start))   AS xact_age_s,
       EXTRACT(EPOCH FROM (now()-state_change)) AS state_age_s,
       COALESCE(query,'''') AS query
FROM pg_stat_activity
WHERE state IN (''active'',''idle in transaction'') AND xact_start IS NOT NULL
  AND client_port IS NOT NULL
  AND pid <> pg_backend_pid()
ORDER BY xact_start
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.long_xact';

-- [health.overview]
--   **修正漏报与计数**：「最老事务」子查询认客户端会话的条件改法同 health.long_xact；连接数改数 pg_stat_activity 里 sessionid <> 0 的条目（原 sum(numbackends) 把线程池空闲工作线程也算进去， 与 health.conn_states 同一口径）。输出列不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  CASE WHEN sum(blks_hit)+sum(blks_read)=0 THEN 100
       ELSE round(100.0*sum(blks_hit)/(sum(blks_hit)+sum(blks_read)),2) END AS cache_hit_pct,
  (SELECT count(*) FROM pg_stat_activity WHERE sessionid <> 0)::bigint AS numbackends,
  (SELECT setting::bigint FROM pg_settings WHERE name=''max_connections'') AS max_conn,
  pg_is_in_recovery() AS in_recovery,
  (SELECT COALESCE(EXTRACT(EPOCH FROM now()-min(xact_start)),0)::bigint
   FROM pg_stat_activity
   WHERE state IN (''active'',''idle in transaction'') AND xact_start IS NOT NULL
     AND client_port IS NOT NULL) AS oldest_xact_s
FROM pg_stat_database;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.overview';

-- [health.stale_stats]
--   **修正漏报**：从没 ANALYZE 过的表 reltuples 与 relpages 都是 0，原门槛与排序把它们截掉了—— 恰恰是统计信息最该更新的那批。新增参数 min_pages（默认 128 页），候选门槛加上按文件大小算的实时页数； 排序改为未分析的在前 → 页数偏离比例 → 体积。只读，判定规则不变。
UPDATE grmp.script_config
   SET script_content   = 'SELECT tbl_name, frozen_pages, cur_pages, frozen_tuples, live_tuples,
       stat_columns, last_analyze, last_autoanalyze
FROM (
  SELECT n.nspname || ''.'' || c.relname AS tbl_name,
         c.relpages AS frozen_pages,
         pg_relation_size(c.oid) / current_setting(''block_size'')::bigint AS cur_pages,
         c.reltuples::bigint AS frozen_tuples,
         COALESCE(t.n_live_tup, 0) AS live_tuples,
         (SELECT count(*) FROM pg_stats s
           WHERE s.schemaname = n.nspname AND s.tablename = c.relname) AS stat_columns,
         COALESCE(to_char(t.last_analyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_analyze,
         COALESCE(to_char(t.last_autoanalyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_autoanalyze
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables t ON t.relid = c.oid
  WHERE c.relkind = ''r'' AND n.nspname NOT IN {{schema_filter}}
    AND (c.reltuples > {{min_rows}}
         OR pg_relation_size(c.oid) / current_setting(''block_size'')::bigint > {{min_pages}})
) x
ORDER BY (stat_columns = 0 OR frozen_pages = 0) DESC,
         CASE WHEN frozen_pages > 0
              THEN CAST(abs(cur_pages - frozen_pages) AS float8) / frozen_pages
              ELSE 0 END DESC,
         cur_pages DESC
LIMIT {{limit}};
',
       parameter_config = '[{"key":"min_rows","value":"","type":"INTEGER","autoAcquire":false},{"key":"min_pages","value":"","type":"INTEGER","autoAcquire":false},{"key":"schema_filter","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.stale_stats';

-- [lockwait.asp_pairs]
--   **修正回看不了**：样本来源由 gs_asp 改为 gs_asp ∪ dbe_perf.local_active_session（内存侧只取将来会落表的 那批、且晚于最后一条落表样本，不重复计数），输出列不变。原来最近几小时的锁等待一律「回看不了」。只读。
UPDATE grmp.script_config
   SET script_content   = 'WITH t AS (
  SELECT a.sessionid, a.block_sessionid, a.event, a.lockmode, a.locktag,
         a.unique_query_id, a.unique_query, a.sample_time
    FROM gs_asp a
   WHERE a.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
     AND a.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
), s AS (
  SELECT * FROM t
   WHERE t.block_sessionid IS NOT NULL AND t.block_sessionid <> 0
  UNION ALL
  SELECT m.sessionid, m.block_sessionid, m.event, m.lockmode, m.locktag,
         m.unique_query_id, m.unique_query, m.sample_time
    FROM dbe_perf.local_active_session m
   WHERE m.need_flush_sample
     AND m.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
     AND m.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
     AND m.sample_time > (SELECT COALESCE(max(t.sample_time), CAST(''1970-01-01'' AS TIMESTAMP)) FROM t)
     AND m.block_sessionid IS NOT NULL AND m.block_sessionid <> 0
), g AS (
  SELECT sessionid, block_sessionid,
         COALESCE(event, '''') AS event, COALESCE(lockmode, '''') AS lockmode,
         COALESCE(locktag, '''') AS locktag, unique_query_id,
         count(*) AS samples, min(sample_time) AS first_seen, max(sample_time) AS last_seen,
         max(substr(COALESCE(unique_query, ''''), 1, 300)) AS query
    FROM s
   GROUP BY sessionid, block_sessionid, COALESCE(event, ''''), COALESCE(lockmode, ''''),
            COALESCE(locktag, ''''), unique_query_id
), d AS (
  SELECT g.*, CASE WHEN g.locktag <> '''' THEN COALESCE(locktag_decode(g.locktag), '''') ELSE '''' END AS decoded
    FROM g
)
SELECT d.sessionid AS waiter_sessionid,
       d.block_sessionid AS holder_sessionid,
       d.event AS wait_event,
       d.lockmode AS waiter_mode,
       d.locktag AS locktag,
       d.decoded AS lock_decoded,
       COALESCE((SELECT n.nspname || ''.'' || c.relname
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE c.oid = CAST(NULLIF(substring(d.decoded from ''relation:([0-9]+)''), '''') AS oid)
                    AND substring(d.decoded from ''database:([0-9]+)'')
                        = (SELECT CAST(oid AS text) FROM pg_database WHERE datname = current_database())), '''') AS lock_object,
       d.unique_query_id AS waiter_sql_id,
       d.query AS waiter_query,
       d.samples AS samples,
       to_char(d.first_seen, ''YYYY-MM-DD HH24:MI:SS'') AS first_seen,
       to_char(d.last_seen, ''YYYY-MM-DD HH24:MI:SS'') AS last_seen
  FROM d
 ORDER BY d.samples DESC, d.sessionid
 LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'lockwait.asp_pairs';

-- [lockwait.asp_sessions]
--   **修正回看不了**：样本来源改法同 lockwait.asp_pairs，输出列不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'WITH t AS (
  SELECT a.sessionid, a.block_sessionid, a.state, a.event, a.unique_query_id, a.unique_query,
         a.application_name, a.client_addr, a.xact_start_time, a.sample_time
    FROM gs_asp a
   WHERE a.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
     AND a.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
), a AS (
  SELECT * FROM t
  UNION ALL
  SELECT m.sessionid, m.block_sessionid, m.state, m.event, m.unique_query_id, m.unique_query,
         m.application_name, m.client_addr, m.xact_start_time, m.sample_time
    FROM dbe_perf.local_active_session m
   WHERE m.need_flush_sample
     AND m.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
     AND m.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
     AND m.sample_time > (SELECT COALESCE(max(t.sample_time), CAST(''1970-01-01'' AS TIMESTAMP)) FROM t)
), w AS (
  SELECT sessionid, block_sessionid FROM a
   WHERE block_sessionid IS NOT NULL AND block_sessionid <> 0
), ids AS (
  SELECT sessionid AS sid FROM w UNION SELECT block_sessionid FROM w
), r AS (
  SELECT a.sessionid, a.state, a.event, a.unique_query_id, a.unique_query,
         a.application_name, a.client_addr, a.xact_start_time, a.sample_time,
         row_number() OVER (PARTITION BY a.sessionid ORDER BY a.sample_time DESC) AS rn,
         count(*) OVER (PARTITION BY a.sessionid) AS samples
    FROM a
   WHERE a.sessionid IN (SELECT sid FROM ids)
)
SELECT r.sessionid AS sessionid,
       COALESCE(r.state, '''') AS state,
       COALESCE(r.event, '''') AS last_event,
       r.unique_query_id AS sql_id,
       substr(COALESCE(r.unique_query, ''''), 1, 300) AS query,
       COALESCE(r.application_name, '''') AS application_name,
       COALESCE(host(r.client_addr), '''') AS client_addr,
       COALESCE(to_char(r.xact_start_time, ''YYYY-MM-DD HH24:MI:SS''), '''') AS xact_start,
       r.samples AS samples
  FROM r
 WHERE r.rn = 1
 ORDER BY r.sessionid
 LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'lockwait.asp_sessions';

-- [lockwait.asp_status]
--   **补充覆盖范围**：时段锁回溯原来只数落表（gs_asp）的样本；最近几小时的活跃会话样本只在内存视图 dbe_perf.local_active_session 里，内存写满才按比例落表。多查 asp_flush_mode、asp_sample_num 两个参数 与内存样本的数量和起止时间，用来如实说明能回看到哪一段。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT name AS item, setting AS value
  FROM pg_settings
 WHERE name IN (''enable_asp'', ''asp_sample_interval'', ''asp_flush_rate'', ''asp_retention_days'',
                ''asp_flush_mode'', ''asp_sample_num'')
UNION ALL
SELECT ''samples_in_window'', CAST(count(*) AS text)
  FROM gs_asp
 WHERE sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
   AND sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
UNION ALL
SELECT ''memory_samples_in_window'', CAST(count(*) AS text)
  FROM dbe_perf.local_active_session m
 WHERE m.need_flush_sample
   AND m.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
   AND m.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
   AND m.sample_time > (SELECT COALESCE(max(t.sample_time), CAST(''1970-01-01'' AS TIMESTAMP))
                          FROM gs_asp t
                         WHERE t.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
                           AND t.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP))
UNION ALL
SELECT ''memory_first'', COALESCE(to_char(min(sample_time), ''YYYY-MM-DD HH24:MI:SS''), '''')
  FROM dbe_perf.local_active_session
UNION ALL
SELECT ''memory_last'', COALESCE(to_char(max(sample_time), ''YYYY-MM-DD HH24:MI:SS''), '''')
  FROM dbe_perf.local_active_session
UNION ALL
SELECT ''table_last'', COALESCE(to_char(max(sample_time), ''YYYY-MM-DD HH24:MI:SS''), '''')
  FROM gs_asp
UNION ALL
SELECT ''now'', to_char(now(), ''YYYY-MM-DD HH24:MI:SS'');
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'lockwait.asp_status';

-- [memanalyze.gucs]
--   **修正误报**：pg_settings 的参数清单补上 enable_resource_record。history 子命令要靠它判断 WLM 历史表 能不能用，原清单漏了它，读不到被当成 off，报告误写「参数没开」。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT name, setting
FROM pg_settings
WHERE name IN (
  ''max_process_memory'', ''shared_buffers'', ''work_mem'', ''maintenance_work_mem'',
  ''max_connections'', ''enable_memory_limit'', ''memory_tracking_mode'',
  ''use_workload_manager'', ''enable_resource_track'', ''resource_track_level'',
  ''resource_track_cost'', ''resource_track_duration'', ''enable_resource_record'',
  ''enable_dynamic_workload'', ''query_max_mem'', ''query_mem'')
ORDER BY name;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'memanalyze.gucs';

-- [topsql.snap_statement]
--   **只加列**：只读 snapshot.snap_summary_statement，输出新增 CPU 与数据 IO 耗时两列， 供时间段 Top SQL 计算等待占比；筛选与排序不变。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  snap_unique_sql_id::text AS unique_sql_id,
  LEFT(REGEXP_REPLACE(COALESCE(NULLIF(REGEXP_REPLACE(max(snap_query), ''^(\s*(--[^\n]*(\n|$)|/\*([^*]|\*+[^*/])*\*+/))+\s*'', ''''), ''''), max(snap_query)),
                     ''\s+'', '' '', ''g''), 80) AS query,
  sum(snap_n_calls) AS calls,
  ROUND(sum(snap_total_elapse_time)/1000000::numeric, 2) AS total_sec,
  ROUND((sum(snap_total_elapse_time)/NULLIF(sum(snap_n_calls),0))/1000::numeric, 2) AS avg_ms,
  sum(snap_n_returned_rows) AS rows,
  ROUND(sum(snap_cpu_time)/1000000::numeric, 2) AS cpu_sec,
  ROUND(sum(snap_data_io_time)/1000000::numeric, 2) AS io_sec
FROM snapshot.snap_summary_statement
WHERE snapshot_id = {{snap}}
GROUP BY snap_unique_sql_id
HAVING sum(snap_n_calls) > 0
ORDER BY sum(snap_total_elapse_time) DESC
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'topsql.snap_statement';

-- [topsql.top_sql]
--   **只加列**：只读 dbe_perf.statement，输出新增 CPU 与数据 IO 耗时两列（cpu_sec / io_sec）， 用于区分「等锁 / 排队的语句」和「自身慢的语句」——实测窗口里两张榜的榜首都是被锁挡住的语句， 真正自身慢的排在后面。筛选条件、原有列和排序参数都不变。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  unique_sql_id::text,
  LEFT(REGEXP_REPLACE(COALESCE(NULLIF(REGEXP_REPLACE(query, ''^(\s*(--[^\n]*(\n|$)|/\*([^*]|\*+[^*/])*\*+/))+\s*'', ''''), ''''), query),
                     ''\s+'', '' '', ''g''), 80) AS query,
  n_calls AS calls,
  ROUND(total_elapse_time/1000000::numeric, 2) AS total_sec,
  ROUND((total_elapse_time/NULLIF(n_calls,0))/1000::numeric, 2) AS avg_ms,
  n_returned_rows AS rows,
  ROUND(cpu_time/1000000::numeric, 2) AS cpu_sec,
  ROUND(data_io_time/1000000::numeric, 2) AS io_sec
FROM (SELECT unique_sql_id,
             max(query)             AS query,
             sum(n_calls)           AS n_calls,
             sum(total_elapse_time) AS total_elapse_time,
             sum(n_returned_rows)   AS n_returned_rows,
             sum(n_blocks_hit)      AS n_blocks_hit,
             sum(n_blocks_fetched)  AS n_blocks_fetched,
             sum(cpu_time)          AS cpu_time,
             sum(data_io_time)      AS data_io_time
        FROM dbe_perf.statement
       GROUP BY unique_sql_id) s
WHERE n_calls > 0
ORDER BY {{order}}
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'topsql.top_sql';

-- [vacuum.oldest_xmin]
--   **修正漏报**：长事务分支认客户端会话的条件改法同 health.long_xact，卡住回收水位的 psycopg2 等客户端事务不再漏掉。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''long_xact'' AS source,
       CAST(pid AS text) AS identifier,
       EXTRACT(EPOCH FROM (now()-xact_start)) AS xmin_age_s,
       ''usename='' || COALESCE(usename,'''') || '' state='' || COALESCE(state,'''') ||
         '' query='' || COALESCE(substr(query,1,200),'''') AS detail
  FROM pg_stat_activity
 WHERE xact_start IS NOT NULL
   AND state IN (''active'',''idle in transaction'')
   AND client_port IS NOT NULL
   AND pid <> pg_backend_pid()
UNION ALL
SELECT ''prepared_xact'' AS source,
       gid AS identifier,
       EXTRACT(EPOCH FROM (now()-prepared)) AS xmin_age_s,
       ''owner='' || COALESCE(owner,'''') || '' database='' || COALESCE(database,'''') AS detail
  FROM pg_prepared_xacts
UNION ALL
SELECT ''replication_slot'' AS source,
       slot_name AS identifier,
       CAST(NULL AS double precision) AS xmin_age_s,
       ''xmin='' || COALESCE(CAST(xmin AS text),'''') ||
         '' catalog_xmin='' || COALESCE(CAST(catalog_xmin AS text),'''') ||
         '' active='' || CAST(active AS text) AS detail
  FROM pg_replication_slots
 WHERE xmin IS NOT NULL OR catalog_xmin IS NOT NULL
ORDER BY xmin_age_s DESC NULLS FIRST;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.oldest_xmin';

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 123：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 本次没有新增脚本。
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('health.conn_concentration', '07030bd38aa881cc62fabe97048cf2ce'),
--     ('health.conn_states', 'b843857b8e76773764b5dd4ed2dd9664'),
--     ('health.long_xact', '70fe0100346efef8bcb44d22c3763cbb'),
--     ('health.overview', 'a7a54d7c41074216f82238a15facb14c'),
--     ('health.stale_stats', '99b65a580c9e88b75c377e10b8cb4765'),
--     ('lockwait.asp_pairs', 'fd806c01d2358444d164368fd20da27a'),
--     ('lockwait.asp_sessions', '2b32f4b336b18b64a2e4b4d0c1c93a92'),
--     ('lockwait.asp_status', '812af8151db28fd7552d20b494f70c4b'),
--     ('memanalyze.gucs', 'ca07802b1c013a92d8b28071509db1e0'),
--     ('topsql.snap_statement', '27e91352ea9c8babd2f92ec07cf5ec77'),
--     ('topsql.top_sql', 'dcdf41fe4dbf95f446307070735e892f'),
--     ('vacuum.oldest_xmin', 'c3e79a3206f4159c0a4e2460b1126555')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('health.stale_stats');
