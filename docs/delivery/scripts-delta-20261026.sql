-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-10-26）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 123 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 126 条）。
--
--   新增   3 条 INSERT
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
-- 一、新增 3 条
-- =====================================================================

-- [sqltune.exec_history] 单条 SQL 每小时最慢多少、执行计划变没变
--   读取：dbe_perf.statement_history
--   用途：按小时归并这条语句被记下的执行,给出平均 / 最大耗时与执行计划指纹;计划只在变化时给出全文,用来判断突然变慢时计划有没有变。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.exec_history', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT hour, runs, avg_ms, max_ms, plan_sig,
       CASE WHEN prev_sig IS NULL OR prev_sig <> plan_sig THEN query_plan END AS query_plan
  FROM (SELECT hour, runs, avg_ms, max_ms, plan_sig, query_plan,
               lag(plan_sig) OVER (ORDER BY hour) AS prev_sig
          FROM (SELECT to_char(hour, ''YYYY-MM-DD HH24:MI'') AS hour, runs,
                       round(avg_us / 1000.0, 1) AS avg_ms,
                       round(max_us / 1000.0, 1) AS max_ms,
                       CASE WHEN coalesce(query_plan, '''') = '''' THEN NULL
                            ELSE md5(regexp_replace(regexp_replace(query_plan, ''\((cost|actual)[^)]*\)'', '''', ''g''),
                                                    ''[0-9]'', '''', ''g'')) END AS plan_sig,
                       query_plan
                  FROM (SELECT date_trunc(''hour'', start_time) AS hour, query_plan,
                               row_number() OVER (PARTITION BY date_trunc(''hour'', start_time)
                                                  ORDER BY (coalesce(query_plan, '''') = ''''), start_time DESC) AS rn,
                               count(*) OVER (PARTITION BY date_trunc(''hour'', start_time)) AS runs,
                               avg(db_time) OVER (PARTITION BY date_trunc(''hour'', start_time)) AS avg_us,
                               max(db_time) OVER (PARTITION BY date_trunc(''hour'', start_time)) AS max_us
                          FROM dbe_perf.statement_history
                         WHERE unique_query_id = {{sid}}
                           AND start_time >= now() - {{days}} * INTERVAL ''1 day'') r
                 WHERE rn = 1) a) b
 ORDER BY hour;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false},{"key":"days","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [sqltune.stat_now] 单条 SQL 的现状(执行次数与耗时)
--   读取：dbe_perf.statement
--   用途：SQL 优化回答的第一段:这条语句自统计清零以来执行了多少次、平均 / 最大 / 最小耗时、CPU 与数据 IO 时间。按 unique_sql_id 只取一条语句,只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.stat_now', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT sum(n_calls) AS calls,
       round(sum(total_elapse_time) / 1000.0, 1) AS total_ms,
       round(sum(total_elapse_time) / 1000.0 / nullif(sum(n_calls), 0), 1) AS avg_ms,
       round(max(max_elapse_time) / 1000.0, 1) AS max_ms,
       round(min(nullif(min_elapse_time, 0)) / 1000.0, 1) AS min_ms,
       round(sum(cpu_time) / 1000.0, 1) AS cpu_ms,
       round(sum(data_io_time) / 1000.0, 1) AS io_ms,
       sum(n_returned_rows) AS returned_rows
  FROM dbe_perf.statement
 WHERE unique_sql_id = {{sid}};
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [sqltune.stat_trend] 单条 SQL 的按小时趋势
--   读取：snapshot.snap_summary_statement / snapshot.snapshot
--   用途：取近 7 天每个 WDR 快照时刻这条语句的累计值,脚本相减得到每小时的次数与平均耗时,判断是否在某个时间段突然变慢。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.stat_trend', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT s.snapshot_id,
       to_char(s.start_ts, ''YYYY-MM-DD HH24:MI:SS'') AS snap_ts,
       sum(t.snap_n_calls) AS calls,
       sum(t.snap_total_elapse_time) AS total_us
  FROM snapshot.snap_summary_statement t
  JOIN snapshot.snapshot s ON s.snapshot_id = t.snapshot_id
 WHERE t.snap_unique_sql_id = {{sid}}
   AND s.start_ts >= now() - {{days}} * INTERVAL ''1 day''
 GROUP BY s.snapshot_id, s.start_ts
 ORDER BY s.snapshot_id;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false},{"key":"days","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- =====================================================================
-- 二、更新 0 条已有脚本的 SQL 正文
-- =====================================================================

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 126：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 新增的 3 条应全部在列：
--   SELECT script_name FROM grmp.script_config WHERE script_name IN ('sqltune.exec_history','sqltune.stat_now','sqltune.stat_trend');
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('-', '-')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
