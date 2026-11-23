-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-05）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 128 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 137 条）。
--
--   新增   9 条 INSERT
--   更新   2 条 UPDATE
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
-- 一、新增 9 条
-- =====================================================================

-- [health.table_writes] 各表的插入 / 更新 / 删除行数（大字段写入折回所属表）
--   读取：pg_stat_all_tables / pg_class / pg_namespace
--   用途：健康检查隔几秒取两次，得出采样期间写入最多的表；大字段（TOAST）的写入折回所属业务表单列标出，用于判断是不是大字段批量写入导致 xlog 涨得快。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.table_writes', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema, "table", is_toast, n_tup_ins, n_tup_upd, n_tup_del, toast_bytes
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [health.write_sessions] 此刻正在执行写入的会话
--   读取：pg_stat_activity
--   用途：正在执行 INSERT / UPDATE / DELETE / MERGE / COPY / CALL 的会话：哪个应用、执行了多久、语句是什么。表级写入计数要等语句结束才累加，长时间的批量写入或存储过程靠这一路说出是谁在写。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.write_sessions', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT sessionid::text AS sessionid,
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [health.write_sql] 写入行数最多的语句
--   读取：dbe_perf.statement
--   用途：按语句汇总插入 + 更新 + 删除的行数（统计重置以来），找出写得最多的业务 SQL。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.write_sql', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT unique_sql_id::text,
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [health.xlog_by_session] 当前事务 xlog 用量最大的会话
--   读取：pg_stat_activity.xlog_quantity
--   用途：GaussDB 上直接给出哪个会话、哪个应用、正在执行哪条 SQL 写了多少 xlog；库里没有这一列时技能如实说明、改看表与 SQL 两路。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.xlog_by_session', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT sessionid::text AS sessionid,
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [health.xlog_position] 此刻的 xlog 写入位置
--   读取：pg_current_xlog_location()
--   用途：健康检查隔几秒取两次，算出 xlog 每秒生成量（此前没有任何检查能测 xlog 涨得多快）。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.xlog_position', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT pg_current_xlog_location()::text AS lsn,
       to_char(clock_timestamp(), ''YYYY-MM-DD HH24:MI:SS.US'') AS at;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [vacuum.xid_lag] 最老 xmin 落后当前事务号多少
--   读取：gs_txid_oldestxmin() / gs_get_next_xid_csn()
--   用途：给「xmin 推不动」一个量：落后几百是正常并发，持续涨到几十万以上说明有事务长时间不结束。不分配事务号。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.xid_lag', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.node_name AS node,
       gs_txid_oldestxmin()::text AS oldest_xmin,
       n.next_xid::text AS next_xid
  FROM gs_get_next_xid_csn() n;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [vacuum.xmin_holder] 卡住死元组回收的事务（持有最老 xmin 的会话）
--   读取：pg_running_xacts / pg_stat_activity / gs_txid_oldestxmin()
--   用途：表膨胀、vacuum 回收不掉时，指出真正持有最老 xmin 的是哪个会话、哪个应用、正在执行什么语句（原来只能列出「此刻有哪些长事务」）。不取客户端地址。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.xmin_holder', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT r.pid::text AS xact_id,
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [wdr.redo_io] 时间段内 redo（xlog）写入量
--   读取：snapshot.snap_global_file_redo_iostat
--   用途：两个 WDR 快照之差：redo 物理写次数与写入块数，对应官方 WDR 报告的 Redo size。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.redo_io', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT COALESCE(SUM(e.snap_phywrts - b.snap_phywrts), 0) AS writes,
       COALESCE(SUM(e.snap_phyblkwrt - b.snap_phyblkwrt), 0) AS blocks
  FROM snapshot.snap_global_file_redo_iostat e
  JOIN snapshot.snap_global_file_redo_iostat b
    ON b.snapshot_id = {{b}} AND b.snap_node_name = e.snap_node_name
 WHERE e.snapshot_id = {{e}};
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [wdr.table_writes] 时间段内各表的插入 / 更新 / 删除行数
--   读取：snapshot.snap_global_stat_all_tables / pg_class / pg_namespace
--   用途：两个 WDR 快照之差，回答「那段时间是哪张表在写」（强制 checkpoint 多、xlog 涨得快时追到业务表）；大字段写入折回所属表。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.table_writes', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema, "table", is_toast, ins, upd, del
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
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false},{"key":"top","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- =====================================================================
-- 二、更新 2 条已有脚本的 SQL 正文
-- =====================================================================

-- [topsql.top_sql]
--   **多取一列**：按语句汇总的写入行数（插入 + 更新 + 删除），支持按写入排 Top SQL（xlog 涨得快、表上死元组多时找写得最多的 SQL）。 筛选、排序方式不变。只读。
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
  ROUND(data_io_time/1000000::numeric, 2) AS io_sec,
  n_tuples_written AS rows_written
FROM (SELECT unique_sql_id,
             max(query)             AS query,
             sum(n_calls)           AS n_calls,
             sum(total_elapse_time) AS total_elapse_time,
             sum(n_returned_rows)   AS n_returned_rows,
             sum(n_blocks_hit)      AS n_blocks_hit,
             sum(n_blocks_fetched)  AS n_blocks_fetched,
             sum(cpu_time)          AS cpu_time,
             sum(data_io_time)      AS data_io_time,
             sum(n_tuples_inserted + n_tuples_updated + n_tuples_deleted) AS n_tuples_written
        FROM dbe_perf.statement
       GROUP BY unique_sql_id) s
WHERE n_calls > 0
ORDER BY {{order}}
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'topsql.top_sql';

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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.dead_tuples';

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 137：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 新增的 9 条应全部在列：
--   SELECT script_name FROM grmp.script_config WHERE script_name IN ('health.table_writes','health.write_sessions','health.write_sql','health.xlog_by_session','health.xlog_position','vacuum.xid_lag','vacuum.xmin_holder','wdr.redo_io','wdr.table_writes');
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('topsql.top_sql', '58a8c892f2405d06fed76d81aae558fe'),
--     ('vacuum.dead_tuples', 'b25f69eeb64c8e9e6bf303511a55391c')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
