-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-10-22）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 118 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 123 条）。
--
--   新增   5 条 INSERT
--   更新   0 条 UPDATE
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
-- 一、新增 5 条
-- =====================================================================

-- [explain.plan_from_history] SQL 历史里最近一次真实执行计划
--   读取：dbe_perf.statement_history（query_plan 列）
--   用途：语句文本是归一化的（带占位符）时 EXPLAIN 不了，改给当时真实执行的计划（带实际耗时与行数），并标明是哪次执行
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.plan_from_history', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema_name,
       to_char(start_time, ''YYYY-MM-DD HH24:MI:SS'') AS start_time,
       round(db_time / 1000.0, 1) AS db_time_ms,
       n_returned_rows,
       query_plan
  FROM dbe_perf.statement_history
 WHERE unique_query_id = {{sid}}
   AND query_plan IS NOT NULL AND query_plan <> ''''
 ORDER BY start_time DESC
 LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [lockwait.asp_pairs] 时段内的锁阻塞对（谁等谁、等什么锁、锁在哪张表、采到几次）
--   读取：gs_asp（活跃会话采样）/ pg_class / pg_namespace / pg_database
--   用途：事后回看某段时间谁挡了谁：锁视图只有此刻，SQL 历史只记锁等待总时长；采样每个样本记着当时在等哪个会话
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.asp_pairs', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH s AS (
  SELECT a.sessionid, a.block_sessionid, a.event, a.lockmode, a.locktag,
         a.unique_query_id, a.unique_query, a.sample_time
    FROM gs_asp a
   WHERE a.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
     AND a.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
     AND a.block_sessionid IS NOT NULL AND a.block_sessionid <> 0
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
', '[{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"end_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [lockwait.asp_sessions] 时段锁回溯涉及的会话（最后状态、应用、客户端、事务开始时间）
--   读取：gs_asp（活跃会话采样）
--   用途：找出阻塞链的根并判断是不是「事务开着不干活」的空闲事务持锁
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.asp_sessions', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH w AS (
  SELECT sessionid, block_sessionid
    FROM gs_asp
   WHERE sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
     AND sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
     AND block_sessionid IS NOT NULL AND block_sessionid <> 0
), ids AS (
  SELECT sessionid AS sid FROM w UNION SELECT block_sessionid FROM w
), r AS (
  SELECT a.sessionid, a.state, a.event, a.unique_query_id, a.unique_query,
         a.application_name, a.client_addr, a.xact_start_time, a.sample_time,
         row_number() OVER (PARTITION BY a.sessionid ORDER BY a.sample_time DESC) AS rn,
         count(*) OVER (PARTITION BY a.sessionid) AS samples
    FROM gs_asp a
   WHERE a.sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
     AND a.sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
     AND a.sessionid IN (SELECT sid FROM ids)
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
', '[{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"end_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [lockwait.asp_status] 活跃会话采样（ASP）的开关与时段内样本数
--   读取：pg_settings / gs_asp
--   用途：时段锁回溯的前提检查：采样没开、过了保留期、还没落表时如实说明「回看不了」，不和「没有锁等待」混为一谈
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.asp_status', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name AS item, setting AS value
  FROM pg_settings
 WHERE name IN (''enable_asp'', ''asp_sample_interval'', ''asp_flush_rate'', ''asp_retention_days'')
UNION ALL
SELECT ''samples_in_window'', CAST(count(*) AS text)
  FROM gs_asp
 WHERE sample_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
   AND sample_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
UNION ALL
SELECT ''now'', to_char(now(), ''YYYY-MM-DD HH24:MI:SS'');
', '[{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"end_time","value":"","type":"DATETIME","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [wdr.plan_in_window] WDR 窗口内某条语句的真实执行计划
--   读取：dbe_perf.statement_history（query_plan 列）
--   用途：WDR 报告 SQL 附录直接附上这段时间执行时的真实计划，不再写「执行计划：本次未取到」
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.plan_in_window', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT to_char(start_time, ''YYYY-MM-DD HH24:MI:SS'') AS start_time,
       round(db_time / 1000.0, 1) AS db_time_ms,
       n_returned_rows,
       query_plan
  FROM dbe_perf.statement_history
 WHERE unique_query_id = {{sid}}
   AND start_time >= CAST(''{{begin_time}}'' AS TIMESTAMP)
   AND start_time <= CAST(''{{end_time}}'' AS TIMESTAMP)
   AND query_plan IS NOT NULL AND query_plan <> ''''
 ORDER BY start_time DESC
 LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false},{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"end_time","value":"","type":"DATETIME","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- =====================================================================
-- 二、更新 0 条已有脚本的 SQL 正文
-- =====================================================================

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 123：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 新增的 5 条应全部在列：
--   SELECT script_name FROM grmp.script_config WHERE script_name IN ('explain.plan_from_history','lockwait.asp_pairs','lockwait.asp_sessions','lockwait.asp_status','wdr.plan_in_window');
-- 更新应已生效（下面两条都应返回 0 行）：
--   SELECT script_name FROM grmp.script_config
--    WHERE script_name LIKE '%plan_text_analyze' AND script_content LIKE '%fasle%';
--   SELECT script_name FROM grmp.script_config
--    WHERE script_name IN ('wdr.top_sql','wdr.file_io','wdr.cache','wdr.db_stat','wdr.db_summary')
--      AND script_content NOT LIKE '%LEFT JOIN%';
