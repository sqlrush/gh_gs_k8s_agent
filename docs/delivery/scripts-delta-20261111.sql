-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-11）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 137 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 138 条）。
--
--   新增   1 条 INSERT
--   更新   71 条 UPDATE
--
-- 全部脚本均为**只读查询**，只访问系统视图与系统表，不读业务数据、
-- 不含 DML/DDL、不修改任何对象。
--
-- 执行库（database_type）：appbusiness 的脚本 parameter_config 最前面带
--   {"key":"dbName","value":"","type":"String","autoAcquire":false}，
-- 中间件按调用时传入的 dbName 切到该业务库执行；postgres 的脚本不带 dbName，在 postgres 库执行。
-- 本文件里的 UPDATE 会同时写 database_type 与 parameter_config，以本文件为准覆盖手工改过的记录。
--
-- 导入前请确认两处与贵方规范的对齐：
--   1. `create_user` / `last_modify_user` 当前填 '999999999'，与上一版保持一致；
--      若贵方要求真实工号，请统一替换。
--   2. id 用 `grmp.script_config_seq.nextval`，不硬编码，避免与现有记录冲突。
--
-- =====================================================================
-- 一、新增 1 条
-- =====================================================================

-- [login.current_db] 登录核实：中间件实际连的库与账号（业务库类，带 dbName）
--   读取：current_database() / current_user
--   用途：登录时带 dbName 调一次，回来的库名等于用户给的业务库，即确认中间件已按 dbName 切库；否则如实提示没切过去的原因。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'login.current_db', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT current_database() AS db, current_user AS usr;
', '[{"key":"dbName","value":"","type":"String","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- =====================================================================
-- 二、更新 71 条已有脚本（SQL 正文 / 参数定义 / 执行库）
-- =====================================================================

-- [config.settings]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT name,
       COALESCE(setting,'''')    AS setting,
       COALESCE(unit,'''')       AS unit,
       COALESCE(vartype,'''')    AS vartype,
       COALESCE(boot_val,'''')   AS boot_val,
       COALESCE(reset_val,'''')  AS reset_val,
       COALESCE(context,'''')    AS context,
       COALESCE(source,'''')     AS source,
       COALESCE(min_val,'''')    AS min_val,
       COALESCE(max_val,'''')    AS max_val,
       COALESCE(enumvals::text,'''') AS enumvals,
       COALESCE(category,'''')   AS category,
       COALESCE(short_desc,'''') AS short_desc
  FROM pg_settings
 ORDER BY name;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'config.settings';

-- [explain.active_pid]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT pid, unique_sql_id, query, query_start
  FROM pg_stat_activity
 WHERE unique_sql_id = {{sql_id}}
   AND state = ''active''
   AND pid <> pg_backend_pid()
 ORDER BY query_start
 LIMIT 1;
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"sql_id","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.active_pid';

-- [explain.from_history]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT schema_name, query
FROM dbe_perf.statement_history
WHERE unique_query_id = {{sid}}
  AND query NOT LIKE ''/* missing SQL statement%''
ORDER BY start_time DESC
LIMIT 1;
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.from_history';

-- [explain.from_statement]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT query, user_name FROM dbe_perf.statement
WHERE unique_sql_id = {{sid}}
  AND query IS NOT NULL
  AND LENGTH(TRIM(query)) > 0
LIMIT 1;
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.from_statement';

-- [explain.kernel_funcs]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''version'' AS item, version() AS detail
UNION ALL
SELECT ''func:'' || p.proname AS item, pg_get_function_arguments(p.oid) AS detail
  FROM pg_proc p
 WHERE p.proname IN (''gs_get_explain'', ''gs_get_kernel_info'');
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.kernel_funcs';

-- [explain.multi_stmt_probe]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO public; SELECT 1 AS ok;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.multi_stmt_probe';

-- [explain.plan_text]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.plan_text';

-- [explain.plan_text_analyze]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.plan_text_analyze';

-- [explain.plan_text_analyze_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.plan_text_analyze_schema';

-- [explain.plan_text_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.plan_text_schema';

-- [explain.relation_schemas]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, c.relname
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'',''f'');
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.relation_schemas';

-- [explain.runtime_plan]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT gs_get_explain({{pid}}::bigint) AS plan;
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"pid","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.runtime_plan';

-- [explain.runtime_plan_int4]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT gs_get_explain({{pid}}::integer) AS plan;
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"pid","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.runtime_plan_int4';

-- [explain.session_by_pid]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT pid, unique_sql_id, query, query_start, state
  FROM pg_stat_activity
 WHERE pid = {{pid}}
 LIMIT 1;
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"pid","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.session_by_pid';

-- [health.invalid_index]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT count(*) AS cnt FROM pg_index WHERE NOT indisvalid;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.invalid_index';

-- [health.live_counters]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''now'' AS item, to_char(now(), ''YYYY-MM-DD HH24:MI:SS'') AS value
UNION ALL
SELECT ''postmaster_start'', to_char(pg_postmaster_start_time(), ''YYYY-MM-DD HH24:MI:SS'')
UNION ALL
SELECT ''stats_reset'', COALESCE(to_char(max(stats_reset), ''YYYY-MM-DD HH24:MI:SS''), '''')
  FROM pg_stat_database
UNION ALL
SELECT ''db_time_us'', COALESCE(sum(value), 0)::text
  FROM dbe_perf.global_instance_time WHERE stat_name = ''DB_TIME''
UNION ALL
SELECT ''cpu_time_us'', COALESCE(sum(value), 0)::text
  FROM dbe_perf.global_instance_time WHERE stat_name = ''CPU_TIME''
UNION ALL
SELECT ''xact_commit'', COALESCE(sum(xact_commit), 0)::text FROM pg_stat_database
UNION ALL
SELECT ''xact_rollback'', COALESCE(sum(xact_rollback), 0)::text FROM pg_stat_database
UNION ALL
SELECT ''deadlocks'', COALESCE(sum(deadlocks), 0)::text FROM pg_stat_database
UNION ALL
SELECT ''temp_bytes'', COALESCE(sum(temp_bytes), 0)::text FROM pg_stat_database
UNION ALL
SELECT ''blks_read'', COALESCE(sum(blks_read), 0)::text FROM pg_stat_database
UNION ALL
SELECT ''blks_hit'', COALESCE(sum(blks_hit), 0)::text FROM pg_stat_database
UNION ALL
SELECT ''ckpt_timed'', COALESCE(sum(checkpoints_timed), 0)::text FROM pg_stat_bgwriter
UNION ALL
SELECT ''ckpt_req'', COALESCE(sum(checkpoints_req), 0)::text FROM pg_stat_bgwriter;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.live_counters';

-- [health.live_waits]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT type  AS wait_class,
       event AS event,
       sum(wait)            AS waits,
       sum(total_wait_time) AS wait_us
  FROM dbe_perf.global_wait_events
 WHERE upper(type) NOT IN (''STATUS'',''NONE'')
 GROUP BY type, event
HAVING sum(wait) > 0;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.live_waits';

-- [health.partitions]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname      AS schema,
       c.relname      AS "table",
       count(*)       AS parts,
       COALESCE(max(p.partstrategy), '''') AS strategy
  FROM pg_partition p
  JOIN pg_class c     ON c.oid = p.parentid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE p.parttype = ''p''
   AND n.nspname NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',
                         ''dbe_pldeveloper'',''cstore'',''pg_toast'')
 GROUP BY n.nspname, c.relname
 ORDER BY parts DESC;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.partitions';

-- [health.snap_counters]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''snap_at'' AS item,
       to_char((SELECT start_ts FROM snapshot.snapshot WHERE snapshot_id = {{snap}}),
               ''YYYY-MM-DD HH24:MI:SS'') AS value
UNION ALL
SELECT ''db_time_us'', COALESCE(sum(snap_value), 0)::text
  FROM snapshot.snap_global_instance_time WHERE snapshot_id = {{snap}} AND snap_stat_name = ''DB_TIME''
UNION ALL
SELECT ''cpu_time_us'', COALESCE(sum(snap_value), 0)::text
  FROM snapshot.snap_global_instance_time WHERE snapshot_id = {{snap}} AND snap_stat_name = ''CPU_TIME''
UNION ALL
SELECT ''xact_commit'', COALESCE(sum(snap_xact_commit), 0)::text
  FROM snapshot.snap_summary_stat_database WHERE snapshot_id = {{snap}}
UNION ALL
SELECT ''xact_rollback'', COALESCE(sum(snap_xact_rollback), 0)::text
  FROM snapshot.snap_summary_stat_database WHERE snapshot_id = {{snap}}
UNION ALL
SELECT ''deadlocks'', COALESCE(sum(snap_deadlocks), 0)::text
  FROM snapshot.snap_summary_stat_database WHERE snapshot_id = {{snap}}
UNION ALL
SELECT ''temp_bytes'', COALESCE(sum(snap_temp_bytes), 0)::text
  FROM snapshot.snap_summary_stat_database WHERE snapshot_id = {{snap}}
UNION ALL
SELECT ''blks_read'', COALESCE(sum(snap_blks_read), 0)::text
  FROM snapshot.snap_summary_stat_database WHERE snapshot_id = {{snap}}
UNION ALL
SELECT ''blks_hit'', COALESCE(sum(snap_blks_hit), 0)::text
  FROM snapshot.snap_summary_stat_database WHERE snapshot_id = {{snap}}
UNION ALL
SELECT ''ckpt_timed'', COALESCE(sum(snap_checkpoints_timed), 0)::text
  FROM snapshot.snap_global_bgwriter_stat WHERE snapshot_id = {{snap}}
UNION ALL
SELECT ''ckpt_req'', COALESCE(sum(snap_checkpoints_req), 0)::text
  FROM snapshot.snap_global_bgwriter_stat WHERE snapshot_id = {{snap}};
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"snap","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.snap_counters';

-- [health.snap_waits]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT snap_type  AS wait_class,
       snap_event AS event,
       sum(snap_wait)            AS waits,
       sum(snap_total_wait_time) AS wait_us
  FROM snapshot.snap_global_wait_events
 WHERE snapshot_id = {{snap}}
   AND upper(snap_type) NOT IN (''STATUS'',''NONE'')
 GROUP BY snap_type, snap_event
HAVING sum(snap_wait) > 0;
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"snap","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.snap_waits';

-- [health.stale_stats]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
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
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"min_rows","value":"","type":"INTEGER","autoAcquire":false},{"key":"min_pages","value":"","type":"INTEGER","autoAcquire":false},{"key":"schema_filter","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.stale_stats';

-- [health.storage]
--   **只量执行账号有权限的库与表空间**:库大小加 has_database_privilege(CONNECT)、表空间大小加 has_tablespace_privilege(CREATE) 或「本库默认表空间」的判断,没权限的那行返回 NULL(报告写「无权限查看」)。原先对每个库、每个表空间都量,受限账号碰到 一个没权限的就整条报错(行方 11-08 测试报告:健康检查 Storage 维度 HTTP 400)。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''database'' AS kind,
       d.datname  AS name,
       CASE WHEN has_database_privilege(d.oid, ''CONNECT'')
            THEN pg_database_size(d.oid)::bigint END AS bytes
  FROM pg_database d
 WHERE d.datallowconn
UNION ALL
SELECT ''tablespace'' AS kind,
       t.spcname    AS name,
       CASE WHEN has_tablespace_privilege(t.oid, ''CREATE'')
              OR t.oid = (SELECT dattablespace FROM pg_database WHERE datname = current_database())
            THEN pg_tablespace_size(t.oid)::bigint END AS bytes
  FROM pg_tablespace t
 ORDER BY bytes DESC NULLS LAST;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.storage';

-- [health.sync_commit]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''peers_configured'' AS item,
       (SELECT count(*)::text FROM pg_settings
         WHERE name LIKE ''replconninfo%'' AND COALESCE(setting,'''') <> '''') AS value
UNION ALL
SELECT ''in_recovery'' AS item, pg_is_in_recovery()::text AS value
UNION ALL
SELECT ''sync_commit'' AS item,
       COALESCE((SELECT setting FROM pg_settings WHERE name=''synchronous_commit''),'''') AS value
UNION ALL
SELECT ''standby_names'' AS item,
       COALESCE((SELECT setting FROM pg_settings WHERE name=''synchronous_standby_names''),'''') AS value;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.sync_commit';

-- [health.table_writes]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT schema, "table", is_toast, n_tup_ins, n_tup_upd, n_tup_del, toast_bytes
  FROM (SELECT COALESCE(pn.nspname, s.schemaname) AS schema,
               COALESCE(pc.relname, s.relname) AS "table",
               (s.schemaname = ''pg_toast'') AS is_toast,
               s.n_tup_ins, s.n_tup_upd, s.n_tup_del,
               CASE WHEN s.schemaname = ''pg_toast'' THEN pg_relation_size(s.relid) END AS toast_bytes,
               s.last_data_changed
          FROM pg_stat_all_tables s
          LEFT JOIN pg_class pc ON s.schemaname = ''pg_toast'' AND pc.reltoastrelid = s.relid
          LEFT JOIN pg_namespace pn ON pn.oid = pc.relnamespace) t
 WHERE schema NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',''dbe_pldeveloper'',''cstore'',''pg_toast'')
   AND n_tup_ins + n_tup_upd + n_tup_del > 0
 ORDER BY last_data_changed DESC NULLS LAST
 LIMIT {{limit}};
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.table_writes';

-- [health.thread_pool]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''enable_thread_pool'' AS item,
       COALESCE((SELECT setting FROM pg_settings WHERE name=''enable_thread_pool''),'''') AS value
UNION ALL
SELECT ''thread_pool_attr'' AS item,
       COALESCE((SELECT setting FROM pg_settings WHERE name=''thread_pool_attr''),'''') AS value
UNION ALL
SELECT ''max_connections'' AS item,
       COALESCE((SELECT setting FROM pg_settings WHERE name=''max_connections''),'''') AS value
UNION ALL
SELECT ''real_sessions'' AS item,
       (SELECT count(*)::text FROM pg_stat_activity WHERE sessionid <> 0) AS value;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.thread_pool';

-- [health.unused_index]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT s.schemaname||''.''||s.indexrelname AS idx_name,
       s.relname AS table_name,
       s.idx_scan,
       s.idx_tup_read,
       s.idx_tup_fetch,
       pg_relation_size(s.indexrelid) AS idx_bytes,
       i.indisprimary,
       i.indisunique,
       COALESCE(ts.seq_scan, 0) AS table_seq_scan,
       COALESCE(ts.idx_scan, 0) AS table_idx_scan
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
LEFT JOIN pg_stat_user_tables ts ON ts.relid = s.relid
WHERE pg_relation_size(s.indexrelid) > {{min_bytes}}
  AND s.schemaname NOT IN {{schema_filter}}
ORDER BY s.idx_scan ASC, pg_relation_size(s.indexrelid) DESC
LIMIT {{limit}};
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"min_bytes","value":"","type":"INTEGER","autoAcquire":false},{"key":"schema_filter","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.unused_index';

-- [health.wal_slots]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT slot_name,
       COALESCE(slot_type,'''')  AS slot_type,
       COALESCE(database,'''')   AS database,
       active::text            AS active,
       pg_xlog_location_diff(pg_current_xlog_location(), restart_lsn)::bigint AS lag_bytes
  FROM pg_replication_slots
 WHERE restart_lsn IS NOT NULL
 ORDER BY lag_bytes DESC;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.wal_slots';

-- [health.write_sessions]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT sessionid::text AS sessionid,
       pid::text AS pid,
       usename,
       application_name,
       EXTRACT(EPOCH FROM (now() - query_start)) AS query_age_s,
       unique_sql_id::text AS unique_sql_id,
       substr(query, 1, 120) AS query
  FROM pg_stat_activity
 WHERE state = ''active''
   AND pid <> pg_backend_pid()
   AND (lower(ltrim(query)) LIKE ''insert%'' OR lower(ltrim(query)) LIKE ''update%''
        OR lower(ltrim(query)) LIKE ''delete%'' OR lower(ltrim(query)) LIKE ''merge%''
        OR lower(ltrim(query)) LIKE ''copy%'' OR lower(ltrim(query)) LIKE ''call%'')
 ORDER BY query_start
 LIMIT {{limit}};
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.write_sessions';

-- [health.write_sql]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT unique_sql_id::text,
       LEFT(REGEXP_REPLACE(COALESCE(NULLIF(REGEXP_REPLACE(query, ''^(\s*(--[^\n]*(\n|$)|/\*([^*]|\*+[^*/])*\*+/))+\s*'', ''''), ''''), query),
                          ''\s+'', '' '', ''g''), 80) AS query,
       n_calls AS calls,
       n_tuples_written AS rows_written,
       ROUND(total_elapse_time/1000000::numeric, 2) AS total_sec
  FROM (SELECT unique_sql_id,
               max(query) AS query,
               sum(n_calls) AS n_calls,
               sum(n_tuples_inserted + n_tuples_updated + n_tuples_deleted) AS n_tuples_written,
               sum(total_elapse_time) AS total_elapse_time
          FROM dbe_perf.statement
         GROUP BY unique_sql_id) s
 WHERE n_tuples_written > 0
 ORDER BY n_tuples_written DESC
 LIMIT {{limit}};
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.write_sql';

-- [health.xlog_by_session]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT sessionid::text AS sessionid,
       pid::text AS pid,
       usename,
       application_name,
       state,
       xlog_quantity,
       unique_sql_id::text AS unique_sql_id,
       substr(query, 1, 120) AS query
  FROM pg_stat_activity
 WHERE xlog_quantity > 0
   AND pid <> pg_backend_pid()
 ORDER BY xlog_quantity DESC
 LIMIT {{limit}};
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.xlog_by_session';

-- [health.xlog_position]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT pg_current_xlog_location()::text AS lsn,
       to_char(clock_timestamp(), ''YYYY-MM-DD HH24:MI:SS.US'') AS at;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.xlog_position';

-- [lockwait.pairs]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT w.pid                         AS waiter_pid,
       COALESCE(w.sessionid, 0)      AS waiter_sessionid,
       w.mode                        AS waiter_mode,
       h.pid                         AS holder_pid,
       COALESCE(h.sessionid, 0)      AS holder_sessionid,
       h.mode                        AS holder_mode,
       w.locktype                    AS locktype,
       COALESCE(n.nspname || ''.'' || c.relname, '''')  AS lock_object,
       COALESCE(w.locktag, '''')       AS locktag,
       round(EXTRACT(EPOCH FROM (now() - wa.query_start))::numeric, 1) AS waiter_wait_s,
       COALESCE(wa.usename, '''')      AS waiter_user,
       COALESCE(wa.application_name, '''') AS waiter_app,
       COALESCE(substr(wa.query, 1, 300), '''')       AS waiter_query,
       COALESCE(ha.state, '''')        AS holder_state,
       COALESCE(ha.usename, '''')      AS holder_user,
       COALESCE(ha.application_name, '''') AS holder_app,
       round(EXTRACT(EPOCH FROM (now() - ha.xact_start))::numeric, 1) AS holder_xact_age_s,
       COALESCE(substr(ha.query, 1, 300), '''')       AS holder_query
  FROM pg_locks w
  JOIN pg_locks h
    ON h.locktag = w.locktag AND h.granted AND h.pid <> w.pid
  LEFT JOIN pg_class c     ON c.oid = w.relation
  LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_activity wa ON wa.pid = w.pid
  LEFT JOIN pg_stat_activity ha ON ha.pid = h.pid
 WHERE w.granted = false
 ORDER BY waiter_wait_s DESC NULLS FIRST -- 未知时长（见上）排最前，不许沉底
 LIMIT {{limit}};
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'lockwait.pairs';

-- [memanalyze.cols_bare]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT a.attname::text AS attname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE c.relname = ''{{relname}}'' AND n.nspname IN ({{schemas}})
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"relname","value":"","type":"STRING","autoAcquire":false},{"key":"schemas","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'memanalyze.cols_bare';

-- [memanalyze.cols_qualified]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT a.attname::text AS attname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = ''{{schema}}'' AND c.relname = ''{{relname}}''
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"relname","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'memanalyze.cols_qualified';

-- [procinfo.locate_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, pg_catalog.has_schema_privilege(n.oid, ''USAGE'') AS usage
FROM pg_namespace n
WHERE n.nspname = ''{{schema}}'';
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'procinfo.locate_schema';

-- [procinfo.locate_search]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''proc'' AS kind, n.nspname, COALESCE(k.pkgname, '''') AS pkgname, p.proname
FROM pg_proc p
LEFT JOIN pg_namespace n ON n.oid = p.pronamespace
LEFT JOIN gs_package k ON k.oid = p.propackageid
WHERE p.proname ILIKE ''%{{name}}%''
  AND (n.nspname IS NULL OR (n.nspname NOT LIKE ''pg_%'' AND n.nspname NOT LIKE ''dbe_%''
       AND n.nspname NOT IN (''information_schema'', ''pkg_service'', ''pkg_util'', ''sqladvisor'', ''db4ai'',
                             ''snapshot'', ''blockchain'', ''cstore'', ''pmk'', ''coverage'')))
UNION ALL
SELECT ''package'' AS kind, n.nspname, k.pkgname, '''' AS proname
FROM gs_package k
LEFT JOIN pg_namespace n ON n.oid = k.pkgnamespace
WHERE COALESCE(length(''{{package}}''), 0) > 0 AND k.pkgname ILIKE ''%{{package}}%''
ORDER BY 1, 2, 3, 4
LIMIT 40;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'procinfo.locate_search';

-- [procinfo.proc_def]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, p.proname, l.lanname, p.prosrc,
       pg_catalog.pg_get_function_arguments(p.oid) AS args,
       COALESCE(k.pkgname, '''') AS package
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
LEFT JOIN gs_package k ON k.oid = p.propackageid
WHERE p.proname = ''{{name}}''
  AND (''{{schema}}'' IS NULL OR ''{{schema}}'' = '''' OR n.nspname = ''{{schema}}'')
  AND (''{{package}}'' IS NULL OR ''{{package}}'' = '''' OR k.pkgname = ''{{package}}'')
ORDER BY (n.nspname = ''public'') DESC, n.nspname, k.pkgname
LIMIT 5;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'procinfo.proc_def';

-- [procinfo.source_adm]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT owner, name, type, line, text
FROM pg_catalog.adm_source
WHERE lower(name) = lower(''{{name}}'')
  AND lower(type) IN (''procedure'', ''function'', ''package body'')
ORDER BY owner, type, line
LIMIT 20001;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"name","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'procinfo.source_adm';

-- [proctune.column_stats]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT schemaname, tablename, attname, n_distinct, null_frac, avg_width, correlation,
       COALESCE(most_common_vals::text, '''') AS most_common_vals,
       COALESCE(most_common_freqs::text, '''') AS most_common_freqs,
       COALESCE(histogram_bounds::text, '''') AS histogram_bounds
FROM pg_stats
WHERE tablename IN ({{names}});
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.column_stats';

-- [proctune.indexes]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname AS schema_name, t.relname AS table_name, i.relname AS index_name,
       ix.indisunique, ix.indisprimary,
       pg_get_indexdef(ix.indexrelid) AS index_def
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname IN ({{names}});
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.indexes';

-- [proctune.locate_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, pg_catalog.has_schema_privilege(n.oid, ''USAGE'') AS usage
FROM pg_namespace n
WHERE n.nspname = ''{{schema}}'';
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.locate_schema';

-- [proctune.locate_search]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''proc'' AS kind, n.nspname, COALESCE(k.pkgname, '''') AS pkgname, p.proname
FROM pg_proc p
LEFT JOIN pg_namespace n ON n.oid = p.pronamespace
LEFT JOIN gs_package k ON k.oid = p.propackageid
WHERE p.proname ILIKE ''%{{name}}%''
  AND (n.nspname IS NULL OR (n.nspname NOT LIKE ''pg_%'' AND n.nspname NOT LIKE ''dbe_%''
       AND n.nspname NOT IN (''information_schema'', ''pkg_service'', ''pkg_util'', ''sqladvisor'', ''db4ai'',
                             ''snapshot'', ''blockchain'', ''cstore'', ''pmk'', ''coverage'')))
UNION ALL
SELECT ''package'' AS kind, n.nspname, k.pkgname, '''' AS proname
FROM gs_package k
LEFT JOIN pg_namespace n ON n.oid = k.pkgnamespace
WHERE COALESCE(length(''{{package}}''), 0) > 0 AND k.pkgname ILIKE ''%{{package}}%''
ORDER BY 1, 2, 3, 4
LIMIT 40;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.locate_search';

-- [proctune.plan_text]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.plan_text';

-- [proctune.plan_text_analyze]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.plan_text_analyze';

-- [proctune.plan_text_analyze_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.plan_text_analyze_schema';

-- [proctune.plan_text_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.plan_text_schema';

-- [proctune.proc_def]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, p.proname, l.lanname, p.prosrc,
       pg_catalog.pg_get_function_arguments(p.oid) AS args,
       COALESCE(k.pkgname, '''') AS package
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
LEFT JOIN gs_package k ON k.oid = p.propackageid
WHERE p.proname = ''{{name}}''
  AND (''{{schema}}'' IS NULL OR ''{{schema}}'' = '''' OR n.nspname = ''{{schema}}'')
  AND (''{{package}}'' IS NULL OR ''{{package}}'' = '''' OR k.pkgname = ''{{package}}'')
ORDER BY (n.nspname = ''public'') DESC, n.nspname, k.pkgname
LIMIT 5;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.proc_def';

-- [proctune.source_adm]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT owner, name, type, line, text
FROM pg_catalog.adm_source
WHERE lower(name) = lower(''{{name}}'')
  AND lower(type) IN (''procedure'', ''function'', ''package body'')
ORDER BY owner, type, line
LIMIT 20001;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"name","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.source_adm';

-- [proctune.tables]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, c.relname, c.relpages,
       c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) / current_setting(''block_size'')::bigint AS curpages,
       c.relkind,
       pg_total_relation_size(c.oid) / 1024.0 / 1024.0 AS size_mb
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'');
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.tables';

-- [sqlreview.indexes]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  n.nspname::text                                              AS schema,
  t.relname::text                                              AS table,
  i.relname::text                                              AS name,
  array_to_string(
    COALESCE((SELECT array_agg(a.attname::text ORDER BY k.ord)
              FROM (SELECT s AS ord,
                           (string_to_array(ix.indkey::text, '' ''))[s]::smallint AS attnum
                    FROM generate_series(
                           1,
                           array_length(string_to_array(ix.indkey::text, '' ''), 1)) s) k
              JOIN pg_attribute a
                ON a.attrelid = t.oid AND a.attnum = k.attnum),
             ARRAY[]::text[]), '','')                            AS columns,
  ix.indisunique                                               AS is_unique,
  ix.indisprimary                                              AS is_primary,
  COALESCE(s.idx_scan, 0)                                      AS scans
FROM pg_index ix
JOIN pg_class i     ON i.oid = ix.indexrelid
JOIN pg_class t     ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = ix.indexrelid
WHERE n.nspname = ''{{schema}}''
ORDER BY t.relname, i.relname;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqlreview.indexes';

-- [sqlreview.tables]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  n.nspname::text                                              AS schema,
  c.relname::text                                              AS table,
  EXISTS (SELECT 1 FROM pg_constraint pk
          WHERE pk.conrelid = c.oid AND pk.contype = ''p'')      AS has_pk,
  array_to_string(
    COALESCE((SELECT array_agg(fk.conname::text)
              FROM pg_constraint fk
              WHERE fk.conrelid = c.oid AND fk.contype = ''f''),
             ARRAY[]::text[]), '','')                            AS fks,
  array_to_string(
    COALESCE((SELECT array_agg(a.attname::text ORDER BY a.attnum)
              FROM pg_attribute a
              WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
             ARRAY[]::text[]), '','')                            AS columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = ''r'' AND n.nspname = ''{{schema}}''
ORDER BY c.relname;
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqlreview.tables';

-- [sqltune.column_stats]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT schemaname, tablename, attname, n_distinct, null_frac, avg_width, correlation,
       COALESCE(most_common_vals::text, '''') AS most_common_vals,
       COALESCE(most_common_freqs::text, '''') AS most_common_freqs,
       COALESCE(histogram_bounds::text, '''') AS histogram_bounds
FROM pg_stats
WHERE tablename IN ({{names}});
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.column_stats';

-- [sqltune.column_types]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname AS schema_name, c.relname AS table_name,
       a.attname, format_type(a.atttypid, NULL) AS type_name
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname IN ({{tables}})
  AND a.attname IN ({{columns}})
  AND a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN (''r'',''v'',''p'',''m'')
  AND n.nspname NOT IN (''pg_catalog'',''information_schema'');
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"tables","value":"","type":"STRING","autoAcquire":false},{"key":"columns","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.column_types';

-- [sqltune.indexes]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname AS schema_name, t.relname AS table_name,
       i.relname AS index_name,
       ix.indisunique,
       ix.indisprimary,
       i.relpages AS index_relpages,
       i.reltuples::bigint AS index_reltuples,
       pg_get_indexdef(ix.indexrelid) AS index_def
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname IN ({{names}});
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.indexes';

-- [sqltune.plan_json]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT JSON) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.plan_json';

-- [sqltune.plan_json_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT JSON) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.plan_json_schema';

-- [sqltune.plan_text]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.plan_text';

-- [sqltune.plan_text_analyze]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.plan_text_analyze';

-- [sqltune.plan_text_analyze_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.plan_text_analyze_schema';

-- [sqltune.plan_text_schema]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.plan_text_schema';

-- [sqltune.stats_freshness]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT schemaname, relname,
       n_live_tup, n_dead_tup,
       COALESCE(to_char(last_analyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_analyze,
       COALESCE(to_char(last_autoanalyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_autoanalyze,
       analyze_count, autoanalyze_count
FROM pg_stat_user_tables
WHERE relname IN ({{names}});
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.stats_freshness';

-- [sqltune.tables]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, c.relname, c.relpages,
       c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) / current_setting(''block_size'')::bigint AS curpages,
       c.relkind,
       pg_total_relation_size(c.oid) / 1024.0 / 1024.0 AS size_mb
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'');
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"names","value":"","type":"STRING","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.tables';

-- [topproc.top_procs]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, p.proname, s.calls,
       ROUND(s.total_time::numeric, 2) AS total_ms,
       ROUND(s.self_time::numeric, 2) AS self_ms
FROM pg_stat_user_functions s
JOIN pg_proc p ON p.oid = s.funcid
JOIN pg_namespace n ON n.oid = p.pronamespace
ORDER BY {{order}} NULLS LAST
LIMIT {{limit}};
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"order","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'topproc.top_procs';

-- [topsql.snap_statement]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
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
       database_type    = 'postgres',
       parameter_config = '[{"key":"snap","value":"","type":"INTEGER","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'topsql.snap_statement';

-- [vacuum.bloat_estimate]
--   执行库:业务库(database_type = appbusiness,parameter_config 最前面加 dbName,中间件按调用时给的库名切库)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname AS schema,
       c.relname AS "table",
       c.relpages::bigint                       AS relpages,
       (pg_relation_size(c.oid) / 8192)::bigint AS real_pages,
       c.reltuples::bigint                      AS reltuples,
       pg_relation_size(c.oid)::bigint          AS real_bytes,
       (SELECT sum(s.avg_width)
          FROM pg_stats s
         WHERE s.schemaname = n.nspname AND s.tablename = c.relname) AS avg_row_width
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = ''r''
   AND n.nspname NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',
                         ''dbe_pldeveloper'',''cstore'',''pg_toast'')
   AND pg_relation_size(c.oid) > 8192 * 128
 ORDER BY pg_relation_size(c.oid) DESC
 LIMIT {{limit}};
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.bloat_estimate';

-- [vacuum.dead_tuples]
--   **多取三列**：这张表的更新、HOT 更新、删除次数（pg_stat_user_tables 的 n_tup_upd / n_tup_hot_upd / n_tup_del）， 报告据此说明死元组来自哪类业务操作。筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname AS schema,
       t.relname AS "table",
       t.n_live_tup AS n_live_tup,
       t.n_dead_tup AS n_dead_tup,
       c.reltuples AS reltuples,
       pg_total_relation_size(c.oid) AS table_bytes,
       EXTRACT(EPOCH FROM (now()-t.last_autovacuum)) AS last_autovacuum_age_s,
       EXTRACT(EPOCH FROM (now()-t.last_vacuum)) AS last_vacuum_age_s,
       t.vacuum_count AS vacuum_count,
       t.autovacuum_count AS autovacuum_count,
       t.n_tup_upd AS n_tup_upd,
       t.n_tup_hot_upd AS n_tup_hot_upd,
       t.n_tup_del AS n_tup_del,
       CASE WHEN ''autovacuum_enabled=false'' = ANY(c.reloptions) THEN false ELSE true END AS autovac_enabled,
       COALESCE(array_to_string(c.reloptions, '',''), '''') AS reloptions
  FROM pg_stat_user_tables t
  JOIN pg_class c ON c.oid = t.relid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',''dbe_pldeveloper'',''cstore'')
 ORDER BY t.n_dead_tup DESC
 LIMIT {{limit}};
',
       database_type    = 'appbusiness',
       parameter_config = '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.dead_tuples';

-- [vacuum.freeze_age]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT d.datname,
       (txid_snapshot_xmin(txid_current_snapshot())
        - d.datfrozenxid64::text::numeric)::bigint      AS freeze_age,
       (SELECT setting::numeric FROM pg_settings
         WHERE name = ''autovacuum_freeze_max_age'')::bigint AS max_age,
       (SELECT setting FROM pg_settings
         WHERE name = ''vacuum_freeze_table_age'')         AS freeze_table_age
  FROM pg_database d
 WHERE d.datallowconn
 ORDER BY freeze_age DESC;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.freeze_age';

-- [vacuum.xid_lag]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.node_name AS node,
       gs_txid_oldestxmin()::text AS oldest_xmin,
       n.next_xid::text AS next_xid
  FROM gs_get_next_xid_csn() n;
',
       database_type    = 'postgres',
       parameter_config = '[]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.xid_lag';

-- [vacuum.xmin_holder]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT r.pid::text AS xact_id,
       r.xmin::text AS xmin,
       (r.xmin::text = gs_txid_oldestxmin()::text) AS is_oldest,
       COALESCE(a1.sessionid, a2.sessionid)::text AS sessionid,
       COALESCE(a1.pid, a2.pid)::text AS pid,
       COALESCE(a1.usename, a2.usename) AS usename,
       COALESCE(a1.application_name, a2.application_name) AS application_name,
       COALESCE(a1.state, a2.state) AS state,
       EXTRACT(EPOCH FROM (now() - COALESCE(a1.xact_start, a2.xact_start))) AS xact_age_s,
       COALESCE(a1.unique_sql_id, a2.unique_sql_id)::text AS unique_sql_id,
       substr(COALESCE(a1.query, a2.query), 1, 200) AS query,
       (COALESCE(a1.client_port, a2.client_port) IS NULL) AS background
  FROM pg_running_xacts r
  LEFT JOIN pg_stat_activity a1 ON a1.sessionid = r.pid
  LEFT JOIN pg_stat_activity a2 ON a1.sessionid IS NULL AND a2.pid = r.pid
 WHERE r.xmin::text <> ''0''
   AND COALESCE(a1.pid, a2.pid, 0) <> pg_backend_pid()
 ORDER BY r.xmin::text::bigint
 LIMIT {{limit}};
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.xmin_holder';

-- [wdr.redo_io]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT COALESCE(SUM(e.snap_phywrts - b.snap_phywrts), 0) AS writes,
       COALESCE(SUM(e.snap_phyblkwrt - b.snap_phyblkwrt), 0) AS blocks
  FROM snapshot.snap_global_file_redo_iostat e
  JOIN snapshot.snap_global_file_redo_iostat b
    ON b.snapshot_id = {{b}} AND b.snap_node_name = e.snap_node_name
 WHERE e.snapshot_id = {{e}};
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'wdr.redo_io';

-- [wdr.table_writes]
--   执行库:postgres(去掉 dbName。读 SQL 执行历史 / WDR 快照 / 实例级视图,切到业务库会查不到或结果相同)
UPDATE grmp.script_config
   SET script_content   = 'SELECT schema, "table", is_toast, ins, upd, del
  FROM (SELECT COALESCE(pn.nspname, e.snap_schemaname) AS schema,
               COALESCE(pc.relname, e.snap_relname) AS "table",
               (e.snap_schemaname = ''pg_toast'') AS is_toast,
               SUM(e.snap_n_tup_ins - COALESCE(b.snap_n_tup_ins, 0)) AS ins,
               SUM(e.snap_n_tup_upd - COALESCE(b.snap_n_tup_upd, 0)) AS upd,
               SUM(e.snap_n_tup_del - COALESCE(b.snap_n_tup_del, 0)) AS del
          FROM snapshot.snap_global_stat_all_tables e
          LEFT JOIN snapshot.snap_global_stat_all_tables b
            ON b.snapshot_id = {{b}} AND b.snap_relid = e.snap_relid
           AND b.db_name = e.db_name AND b.snap_node_name = e.snap_node_name
          LEFT JOIN pg_class pc ON e.snap_schemaname = ''pg_toast'' AND pc.reltoastrelid = e.snap_relid
          LEFT JOIN pg_namespace pn ON pn.oid = pc.relnamespace
         WHERE e.snapshot_id = {{e}}
         GROUP BY 1, 2, 3) t
 WHERE schema NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',''dbe_pldeveloper'',''cstore'',''pg_toast'')
   AND ins + upd + del > 0
 ORDER BY ins + upd + del DESC
 LIMIT {{top}};
',
       database_type    = 'postgres',
       parameter_config = '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false},{"key":"top","value":"","type":"INTEGER","autoAcquire":false}]',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'wdr.table_writes';

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 138：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 新增的 1 条应全部在列：
--   SELECT script_name FROM grmp.script_config WHERE script_name IN ('login.current_db');
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('config.settings', 'a244698242f95449774374f2b2e98eb0'),
--     ('explain.active_pid', 'd3483e221fa6ae60e7dabdf52bb00a3b'),
--     ('explain.from_history', 'd708b58815392027d90a6ee775c1985a'),
--     ('explain.from_statement', '356782475dd1118b46ddcb1627b3b4dd'),
--     ('explain.kernel_funcs', '36822739ae22eb72626287cd3523fd92'),
--     ('explain.multi_stmt_probe', '33bec0ab47464780193a8853cda2de32'),
--     ('explain.plan_text', 'f8cb32f9e885e362cfc278073ba7f574'),
--     ('explain.plan_text_analyze', 'f8cb32f9e885e362cfc278073ba7f574'),
--     ('explain.plan_text_analyze_schema', 'cc16b2d5046f3302f61fa96bd32b7d26'),
--     ('explain.plan_text_schema', 'cc16b2d5046f3302f61fa96bd32b7d26'),
--     ('explain.relation_schemas', 'b9a1c02357d181ba94f6e21b3fb72c1b'),
--     ('explain.runtime_plan', '6506e27cc9af92b677ac2c45f890459a'),
--     ('explain.runtime_plan_int4', 'f1dcf24d0082447744ad3117498a4017'),
--     ('explain.session_by_pid', 'f2b87e07779e34854ff086462794b658'),
--     ('health.invalid_index', '9020e8dbcccec2962e57762cb1f27455'),
--     ('health.live_counters', '207db942f5ae8a5bc0228d1e2b8cc4ed'),
--     ('health.live_waits', '3f88e98d1aafdf39e52d793c2da0bda5'),
--     ('health.partitions', 'c4799de87ebf25e8275c5abd5808442d'),
--     ('health.snap_counters', '0bcad20e9ba51e794592d67e62d89600'),
--     ('health.snap_waits', 'ba1d5a9893ad73ac5e174952b5a9314e'),
--     ('health.stale_stats', '99b65a580c9e88b75c377e10b8cb4765'),
--     ('health.storage', '15f066d47238c5a1453e7d04958715ce'),
--     ('health.sync_commit', 'c20f25b4584600fb9240efd34a7b80fe'),
--     ('health.table_writes', '66d2841e77020fea5fa65599f738c730'),
--     ('health.thread_pool', '0a16251e16bb226019434210d4e4671b'),
--     ('health.unused_index', '787736fde483c0f10e2c048d906a1df9'),
--     ('health.wal_slots', 'cb46d95d733dee42ac92d93c0d6923ae'),
--     ('health.write_sessions', '5847611e55f7600bb5989d4a0fed33f2'),
--     ('health.write_sql', '12d155e36a227f9398ab1e615533e8a9'),
--     ('health.xlog_by_session', '2d984b2c3a694e5f578e9ddad769c948'),
--     ('health.xlog_position', '565c39e38ec6fc3ce25642cf5eefe29d'),
--     ('lockwait.pairs', '4468056a1dda6346a21f26c035191b3c'),
--     ('memanalyze.cols_bare', 'c9d84c8f6072b99fdd806b6ca49516da'),
--     ('memanalyze.cols_qualified', 'f2ebbad7d8c1a2a6998b62c9be7cfe8c'),
--     ('procinfo.locate_schema', '9dbcda310ea6ff7c9b7c2f4225e7bb3a'),
--     ('procinfo.locate_search', '67c7c68f7b161b0d289532b92896b63c'),
--     ('procinfo.proc_def', '03ca6d806a6dda5a72f3b6b5c999cf42'),
--     ('procinfo.source_adm', '450739529535f131e466282d0bb9eb16'),
--     ('proctune.column_stats', '541ff3d1429a07fc90f2c7421f0f8abc'),
--     ('proctune.indexes', '0984e161d07db894ca56ef68d457e1bf'),
--     ('proctune.locate_schema', '9dbcda310ea6ff7c9b7c2f4225e7bb3a'),
--     ('proctune.locate_search', '67c7c68f7b161b0d289532b92896b63c'),
--     ('proctune.plan_text', 'f8cb32f9e885e362cfc278073ba7f574'),
--     ('proctune.plan_text_analyze', 'f8cb32f9e885e362cfc278073ba7f574'),
--     ('proctune.plan_text_analyze_schema', 'cc16b2d5046f3302f61fa96bd32b7d26'),
--     ('proctune.plan_text_schema', 'cc16b2d5046f3302f61fa96bd32b7d26'),
--     ('proctune.proc_def', '03ca6d806a6dda5a72f3b6b5c999cf42'),
--     ('proctune.source_adm', '450739529535f131e466282d0bb9eb16'),
--     ('proctune.tables', 'fa4e74ee99c92d537dd056be0e209c36'),
--     ('sqlreview.indexes', 'b184ca749839056b4e4a45ba1ca19f59'),
--     ('sqlreview.tables', 'c83e7ff52125f0639faee65efb632f80'),
--     ('sqltune.column_stats', '541ff3d1429a07fc90f2c7421f0f8abc'),
--     ('sqltune.column_types', '86a7d89b3e8a3175bcca39e0ceeb8e2d'),
--     ('sqltune.indexes', 'e4f2be2ee2b0eff5cd2cd8e53c1227a0'),
--     ('sqltune.plan_json', 'aba223844aafe19d400b698deaa5d84d'),
--     ('sqltune.plan_json_schema', '3c788d687598b2ea870896953bf1767b'),
--     ('sqltune.plan_text', 'f8cb32f9e885e362cfc278073ba7f574'),
--     ('sqltune.plan_text_analyze', 'f8cb32f9e885e362cfc278073ba7f574'),
--     ('sqltune.plan_text_analyze_schema', 'cc16b2d5046f3302f61fa96bd32b7d26'),
--     ('sqltune.plan_text_schema', 'cc16b2d5046f3302f61fa96bd32b7d26'),
--     ('sqltune.stats_freshness', 'e3df06a7c8a4f9e332530eeddb817963'),
--     ('sqltune.tables', 'fa4e74ee99c92d537dd056be0e209c36'),
--     ('topproc.top_procs', '351cf129a0f8962ae36f073f28046c9e'),
--     ('topsql.snap_statement', '27e91352ea9c8babd2f92ec07cf5ec77'),
--     ('vacuum.bloat_estimate', 'be88652a701a019d042776173cd6eb1c'),
--     ('vacuum.dead_tuples', 'b25f69eeb64c8e9e6bf303511a55391c'),
--     ('vacuum.freeze_age', 'c67d1b45a71f34abf6fa988e9d4c3ca3'),
--     ('vacuum.xid_lag', 'f1e63de79e4d157628b798840642a7c8'),
--     ('vacuum.xmin_holder', '4c1d617d2bbdf35592c1181bfc1e16d0'),
--     ('wdr.redo_io', 'ea421e637cdad601389b91847b040474'),
--     ('wdr.table_writes', 'edf2960a5e3d5ea05622656cf77e206f')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('config.settings','explain.active_pid','explain.from_history','explain.from_statement','explain.kernel_funcs','explain.multi_stmt_probe','explain.plan_text','explain.plan_text_analyze','explain.plan_text_analyze_schema','explain.plan_text_schema','explain.relation_schemas','explain.runtime_plan','explain.runtime_plan_int4','explain.session_by_pid','health.invalid_index','health.live_counters','health.live_waits','health.partitions','health.snap_counters','health.snap_waits','health.stale_stats','health.storage','health.sync_commit','health.table_writes','health.thread_pool','health.unused_index','health.wal_slots','health.write_sessions','health.write_sql','health.xlog_by_session','health.xlog_position','lockwait.pairs','memanalyze.cols_bare','memanalyze.cols_qualified','procinfo.locate_schema','procinfo.locate_search','procinfo.proc_def','procinfo.source_adm','proctune.column_stats','proctune.indexes','proctune.locate_schema','proctune.locate_search','proctune.plan_text','proctune.plan_text_analyze','proctune.plan_text_analyze_schema','proctune.plan_text_schema','proctune.proc_def','proctune.source_adm','proctune.tables','sqlreview.indexes','sqlreview.tables','sqltune.column_stats','sqltune.column_types','sqltune.indexes','sqltune.plan_json','sqltune.plan_json_schema','sqltune.plan_text','sqltune.plan_text_analyze','sqltune.plan_text_analyze_schema','sqltune.plan_text_schema','sqltune.stats_freshness','sqltune.tables','topproc.top_procs','topsql.snap_statement','vacuum.bloat_estimate','vacuum.dead_tuples','vacuum.freeze_age','vacuum.xid_lag','vacuum.xmin_holder','wdr.redo_io','wdr.table_writes');
-- 执行库应与本版一致（appbusiness 应为 46 条，且都以 dbName 开头）：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness';
--   SELECT script_name FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness'
--      AND parameter_config NOT LIKE '[{"key":"dbName",%';     -- 应返回 0 行
