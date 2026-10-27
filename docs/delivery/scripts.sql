
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.plan_text', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.plan_text_analyze', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.archive_mode', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT setting FROM pg_settings WHERE name=''archive_mode'';
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.bgwriter', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT checkpoints_timed, checkpoints_req FROM pg_stat_bgwriter;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.db_concurrency', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT deadlocks, xact_commit, xact_rollback
FROM pg_stat_database WHERE datname=current_database();
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.invalid_index', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT count(*) AS cnt FROM pg_index WHERE NOT indisvalid;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.prepared_xacts', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT count(*) AS cnt FROM pg_prepared_xacts;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.replication', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT application_name,
       COALESCE(client_addr::text,'''') AS client_addr,
       state, sync_state,
       pg_xlog_location_diff(sender_sent_location, receiver_replay_location)::bigint AS lag_bytes
FROM pg_stat_replication;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.slow_sql', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT
  unique_sql_id::text,
  LEFT(REGEXP_REPLACE(query, ''\s+'', '' '', ''g''), 180) AS query,
  n_calls AS calls,
  ROUND((total_elapse_time/NULLIF(n_calls,0))/1000::numeric, 2) AS avg_ms,
  ROUND(total_elapse_time/1000000::numeric, 2) AS total_sec,
  ROUND(cpu_time/1000000::numeric, 2) AS cpu_sec,
  n_returned_rows AS rows
FROM dbe_perf.statement
WHERE (total_elapse_time/NULLIF(n_calls,0))/1000 > {{threshold_ms}}
  AND n_calls > 0
ORDER BY total_elapse_time/NULLIF(n_calls,0) DESC
LIMIT {{limit}};
', '[{"key":"threshold_ms","value":"","type":"INTEGER","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.stats_window', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT COALESCE(to_char(stats_reset, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS stats_reset,
       COALESCE(EXTRACT(EPOCH FROM (now() - stats_reset)), -1) AS window_seconds
FROM pg_stat_database
WHERE datname = current_database();
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.unused_index', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT s.schemaname||''.''||s.indexrelname AS idx_name,
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
', '[{"key":"min_bytes","value":"","type":"INTEGER","autoAcquire":false},{"key":"schema_filter","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.activity', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT sessionid, pid, usename, application_name, state, query FROM pg_stat_activity;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.cols_bare', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT a.attname::text AS attname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE c.relname = ''{{relname}}'' AND n.nspname IN ({{schemas}})
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
', '[{"key":"relname","value":"","type":"STRING","autoAcquire":false},{"key":"schemas","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.cols_qualified', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT a.attname::text AS attname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = ''{{schema}}'' AND c.relname = ''{{relname}}''
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"relname","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.context', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT contextname, sum(totalsize) AS totalsize,
       sum(freesize) AS freesize, sum(usedsize) AS usedsize
FROM (SELECT contextname, totalsize, freesize, usedsize FROM gs_session_memory_detail) t
GROUP BY contextname ORDER BY 4 DESC NULLS LAST
LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.instance', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT memorytype, memorymbytes FROM gs_total_memory_detail;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.session', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT sessid, init_mem, used_mem, peak_mem FROM dbe_perf.session_memory
ORDER BY peak_mem DESC NULLS LAST LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.wlm_operator', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT queryid, plan_node_id, plan_node_name, duration, NULL AS estimate_memory, NULL AS memory_used, max_peak_memory, average_peak_memory, NULL AS spill_size, warning FROM gs_wlm_operator_statistics
ORDER BY max_peak_memory DESC NULLS LAST LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.wlm_operator_hist', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT queryid, plan_node_id, plan_node_name, duration, NULL AS estimate_memory, NULL AS memory_used, max_peak_memory, average_peak_memory, NULL AS spill_size, warning FROM gs_wlm_operator_history
ORDER BY max_peak_memory DESC NULLS LAST LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.wlm_sql', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT queryid, query, start_time, duration, estimate_memory, NULL AS used_memory, max_peak_memory, average_peak_memory, spill_info FROM gs_wlm_session_statistics
ORDER BY max_peak_memory DESC NULLS LAST LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.wlm_sql_hist', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT queryid, query, start_time, duration, estimate_memory, NULL AS used_memory, max_peak_memory, average_peak_memory, spill_info FROM gs_wlm_session_history
ORDER BY max_peak_memory DESC NULLS LAST LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'procinfo.key_gucs', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name, setting, COALESCE(unit, '''') AS unit
FROM pg_settings
WHERE name IN (
  ''work_mem'', ''maintenance_work_mem'', ''shared_buffers'',
  ''effective_cache_size'', ''random_page_cost'', ''seq_page_cost'',
  ''cpu_tuple_cost'', ''cpu_index_tuple_cost'', ''cpu_operator_cost'', ''block_size'',
  ''query_dop'',
  ''max_parallel_workers_per_gather'', ''from_collapse_limit'',
  ''join_collapse_limit'', ''geqo_threshold'', ''default_statistics_target'')
ORDER BY name;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.db_version', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT version() AS version;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.key_gucs', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name, setting, COALESCE(unit, '''') AS unit
FROM pg_settings
WHERE name IN (
  ''work_mem'', ''maintenance_work_mem'', ''shared_buffers'',
  ''effective_cache_size'', ''random_page_cost'', ''seq_page_cost'',
  ''cpu_tuple_cost'', ''cpu_index_tuple_cost'', ''cpu_operator_cost'', ''block_size'',
  ''query_dop'',
  ''max_parallel_workers_per_gather'', ''from_collapse_limit'',
  ''join_collapse_limit'', ''geqo_threshold'', ''default_statistics_target'')
ORDER BY name;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.plan_text', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.plan_text_analyze', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.sql_from_history', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema_name, query
FROM dbe_perf.statement_history
WHERE unique_query_id = {{sid}}
  AND query NOT LIKE ''/* missing SQL statement%''
ORDER BY start_time DESC
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.tables', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, c.relname, c.relpages,
       c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) / current_setting(''block_size'')::bigint AS curpages,
       c.relkind,
       pg_total_relation_size(c.oid) / 1024.0 / 1024.0 AS size_mb
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'');
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'slowsql.slow_sql', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT
  unique_sql_id::text,
  LEFT(REGEXP_REPLACE(query, ''\s+'', '' '', ''g''), 180) AS query,
  n_calls AS calls,
  ROUND((total_elapse_time/NULLIF(n_calls,0))/1000::numeric, 2) AS avg_ms,
  ROUND(total_elapse_time/1000000::numeric, 2) AS total_sec,
  ROUND(cpu_time/1000000::numeric, 2) AS cpu_sec,
  n_returned_rows AS rows
FROM dbe_perf.statement
WHERE (total_elapse_time/NULLIF(n_calls,0))/1000 > {{threshold_ms}}
  AND n_calls > 0 AND last_updated >= CAST(''{{begin_time}}'' AS TIMESTAMP)
ORDER BY total_elapse_time/NULLIF(n_calls,0) DESC
LIMIT {{limit}};
', '[{"key":"threshold_ms","value":"","type":"INTEGER","autoAcquire":false},{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqlfetch.from_history', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema_name, query
FROM dbe_perf.statement_history
WHERE unique_query_id = {{sid}}
  AND query NOT LIKE ''/* missing SQL statement%''
ORDER BY start_time DESC
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqlreview.from_history', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema_name, query
FROM dbe_perf.statement_history
WHERE unique_query_id = {{sid}}
  AND query NOT LIKE ''/* missing SQL statement%''
ORDER BY start_time DESC
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqlreview.indexes', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT
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
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqlreview.tables', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT
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
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqlreview.top_sql', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT unique_sql_id::text, query
FROM dbe_perf.statement
WHERE n_calls > 0 AND query IS NOT NULL AND LENGTH(TRIM(query)) > 0
ORDER BY total_elapse_time DESC
LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.from_history', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema_name, query
FROM dbe_perf.statement_history
WHERE unique_query_id = {{sid}}
  AND query NOT LIKE ''/* missing SQL statement%''
ORDER BY start_time DESC
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.key_gucs', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name, setting, COALESCE(unit, '''') AS unit
FROM pg_settings
WHERE name IN (
  ''work_mem'', ''maintenance_work_mem'', ''shared_buffers'',
  ''effective_cache_size'', ''random_page_cost'', ''seq_page_cost'',
  ''cpu_tuple_cost'', ''cpu_index_tuple_cost'', ''cpu_operator_cost'', ''block_size'',
  ''query_dop'',
  ''max_parallel_workers_per_gather'', ''from_collapse_limit'',
  ''join_collapse_limit'', ''geqo_threshold'', ''default_statistics_target'')
ORDER BY name;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.plan_json', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT JSON) {{sql}}
', '[{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.plan_text', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.plan_text_analyze', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.stats_freshness', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schemaname, relname,
       n_live_tup, n_dead_tup,
       COALESCE(to_char(last_analyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_analyze,
       COALESCE(to_char(last_autoanalyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_autoanalyze,
       analyze_count, autoanalyze_count
FROM pg_stat_user_tables
WHERE relname IN ({{names}});
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.tables', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, c.relname, c.relpages,
       c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) / current_setting(''block_size'')::bigint AS curpages,
       c.relkind,
       pg_total_relation_size(c.oid) / 1024.0 / 1024.0 AS size_mb
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'');
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.version', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT version() AS version;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'topproc.top_procs', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, p.proname, s.calls,
       ROUND(s.total_time::numeric, 2) AS total_ms,
       ROUND(s.self_time::numeric, 2) AS self_ms
FROM pg_stat_user_functions s
JOIN pg_proc p ON p.oid = s.funcid
JOIN pg_namespace n ON n.oid = p.pronamespace
ORDER BY {{order}} NULLS LAST
LIMIT {{limit}};
', '[{"key":"order","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.cache', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT db_name, snap_schemaname, snap_relname,
                  (COALESCE(snap_heap_blks_read,0)+COALESCE(snap_idx_blks_read,0)) AS phys,
                  (COALESCE(snap_heap_blks_hit,0)+COALESCE(snap_idx_blks_hit,0))   AS logi
             FROM snapshot.snap_summary_statio_all_tables WHERE snapshot_id={{b}}),
     e AS (SELECT db_name, snap_schemaname, snap_relname,
                  (COALESCE(snap_heap_blks_read,0)+COALESCE(snap_idx_blks_read,0)) AS phys,
                  (COALESCE(snap_heap_blks_hit,0)+COALESCE(snap_idx_blks_hit,0))   AS logi
             FROM snapshot.snap_summary_statio_all_tables WHERE snapshot_id={{e}})
SELECT e.snap_relname,
       (e.phys - COALESCE(b.phys, 0)) AS phys_read,
       (e.logi - COALESCE(b.logi, 0)) AS logical_read
FROM e LEFT JOIN b USING (db_name, snap_schemaname, snap_relname)
WHERE (e.phys - COALESCE(b.phys, 0)) > 0
ORDER BY phys_read DESC LIMIT {{top}};
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false},{"key":"top","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.checkpoint', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_node_name, snap_checkpoints_timed AS timed, snap_checkpoints_req AS req
             FROM snapshot.snap_global_bgwriter_stat WHERE snapshot_id={{b}}),
     e AS (SELECT snap_node_name, snap_checkpoints_timed AS timed, snap_checkpoints_req AS req
             FROM snapshot.snap_global_bgwriter_stat WHERE snapshot_id={{e}})
SELECT COALESCE(SUM(e.timed-b.timed),0) AS checkpoints_timed,
       COALESCE(SUM(e.req-b.req),0)     AS checkpoints_req
FROM e JOIN b USING (snap_node_name);
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.db_stat', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_datname, snap_xact_commit, snap_xact_rollback, snap_deadlocks,
                  snap_temp_bytes, snap_blks_hit, snap_blks_read
             FROM snapshot.snap_summary_stat_database WHERE snapshot_id={{b}}),
     e AS (SELECT snap_datname, snap_xact_commit, snap_xact_rollback, snap_deadlocks,
                  snap_temp_bytes, snap_blks_hit, snap_blks_read
             FROM snapshot.snap_summary_stat_database WHERE snapshot_id={{e}})
SELECT COALESCE(SUM(e.snap_xact_commit - COALESCE(b.snap_xact_commit, 0)),0)     AS xact_commit,
       COALESCE(SUM(e.snap_xact_rollback - COALESCE(b.snap_xact_rollback, 0)),0) AS xact_rollback,
       COALESCE(SUM(e.snap_deadlocks - COALESCE(b.snap_deadlocks, 0)),0)         AS deadlocks,
       COALESCE(SUM(e.snap_temp_bytes - COALESCE(b.snap_temp_bytes, 0)),0)       AS temp_bytes,
       COALESCE(SUM(e.snap_blks_hit - COALESCE(b.snap_blks_hit, 0)),0)           AS blks_hit,
       COALESCE(SUM(e.snap_blks_read - COALESCE(b.snap_blks_read, 0)),0)         AS blks_read
FROM e LEFT JOIN b USING (snap_datname);
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.db_summary', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_datname, snap_xact_commit, snap_blks_read, snap_blks_hit
             FROM snapshot.snap_summary_stat_database WHERE snapshot_id={{b}}),
     e AS (SELECT snap_datname, snap_xact_commit, snap_blks_read, snap_blks_hit
             FROM snapshot.snap_summary_stat_database WHERE snapshot_id={{e}})
SELECT COALESCE(SUM(e.snap_xact_commit - COALESCE(b.snap_xact_commit, 0)),0) AS xact_commit,
       COALESCE(SUM(e.snap_blks_read - COALESCE(b.snap_blks_read, 0)),0)     AS blks_read,
       COALESCE(SUM(e.snap_blks_hit - COALESCE(b.snap_blks_hit, 0)),0)       AS blks_hit
FROM e LEFT JOIN b USING (snap_datname);
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.file_io', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_filenum, snap_dbid, snap_spcid, snap_phyrds AS reads, snap_phywrts AS writes
             FROM snapshot.snap_summary_file_iostat WHERE snapshot_id={{b}}),
     e AS (SELECT snap_filenum, snap_dbid, snap_spcid, snap_phyrds AS reads, snap_phywrts AS writes
             FROM snapshot.snap_summary_file_iostat WHERE snapshot_id={{e}})
SELECT (''db''||e.snap_dbid||''/spc''||e.snap_spcid||''/f''||e.snap_filenum) AS filename,
       (e.reads  - COALESCE(b.reads,  0)) AS reads,
       (e.writes - COALESCE(b.writes, 0)) AS writes
FROM e LEFT JOIN b USING (snap_filenum, snap_dbid, snap_spcid)
WHERE (e.reads  - COALESCE(b.reads,  0)) > 0
   OR (e.writes - COALESCE(b.writes, 0)) > 0
ORDER BY reads DESC LIMIT {{top}};
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false},{"key":"top","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.load_profile', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_unique_sql_id AS sid, sum(snap_total_elapse_time) AS t, sum(snap_cpu_time) AS c
             FROM snapshot.snap_summary_statement WHERE snapshot_id={{b}} GROUP BY snap_unique_sql_id),
     e AS (SELECT snap_unique_sql_id AS sid, sum(snap_total_elapse_time) AS t, sum(snap_cpu_time) AS c
             FROM snapshot.snap_summary_statement WHERE snapshot_id={{e}} GROUP BY snap_unique_sql_id)
SELECT COALESCE(SUM(e.t-b.t),0)  AS db_time_us,
       COALESCE(SUM(e.c-b.c),0)  AS cpu_time_us
FROM e JOIN b USING (sid);
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.native_report', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT generate_wdr_report({{begin}}, {{end}}, ''all'', ''{{scope}}'', ''{{node}}'') AS report_line;
', '[{"key":"begin","value":"","type":"INTEGER","autoAcquire":false},{"key":"end","value":"","type":"INTEGER","autoAcquire":false},{"key":"scope","value":"","type":"STRING","autoAcquire":false},{"key":"node","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.node_name', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SHOW pgxc_node_name;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.snapshots', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT snapshot_id,
       to_char(start_ts,''YYYY-MM-DD HH24:MI:SS'') AS start_ts,
       to_char(end_ts,''YYYY-MM-DD HH24:MI:SS'')   AS end_ts,
       round(EXTRACT(EPOCH FROM (end_ts-start_ts))/60)::bigint AS dur_min
FROM snapshot.snapshot ORDER BY snapshot_id DESC LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.top_sql', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_unique_sql_id AS sid, max(snap_query) AS query,
                  sum(snap_n_calls) AS calls, sum(snap_total_elapse_time) AS elapsed, sum(snap_cpu_time) AS cpu,
                  sum(COALESCE(snap_sort_spill_size,0)+COALESCE(snap_hash_spill_size,0)) AS spill,
                  sum(snap_n_blocks_fetched-snap_n_blocks_hit) AS phys
             FROM snapshot.snap_summary_statement WHERE snapshot_id={{b}} GROUP BY snap_unique_sql_id),
     e AS (SELECT snap_unique_sql_id AS sid, max(snap_query) AS query,
                  sum(snap_n_calls) AS calls, sum(snap_total_elapse_time) AS elapsed, sum(snap_cpu_time) AS cpu,
                  sum(COALESCE(snap_sort_spill_size,0)+COALESCE(snap_hash_spill_size,0)) AS spill,
                  sum(snap_n_blocks_fetched-snap_n_blocks_hit) AS phys
             FROM snapshot.snap_summary_statement WHERE snapshot_id={{e}} GROUP BY snap_unique_sql_id)
SELECT e.sid, e.query,
       (e.calls   - COALESCE(b.calls,   0)) AS calls,
       (e.elapsed - COALESCE(b.elapsed, 0)) AS elapsed_us,
       (e.cpu     - COALESCE(b.cpu,     0)) AS cpu_us,
       (e.spill   - COALESCE(b.spill,   0)) AS spill_kb,
       (e.phys    - COALESCE(b.phys,    0)) AS phys_blocks
FROM e LEFT JOIN b USING (sid)
WHERE (e.elapsed - COALESCE(b.elapsed, 0)) > 0
ORDER BY elapsed_us DESC LIMIT {{top}};
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false},{"key":"top","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.waits', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_type AS wait_class, snap_event AS event, sum(snap_wait) AS waits, sum(snap_total_wait_time) AS wt
             FROM snapshot.snap_global_wait_events WHERE snapshot_id={{b}} GROUP BY snap_type, snap_event),
     e AS (SELECT snap_type AS wait_class, snap_event AS event, sum(snap_wait) AS waits, sum(snap_total_wait_time) AS wt
             FROM snapshot.snap_global_wait_events WHERE snapshot_id={{e}} GROUP BY snap_type, snap_event)
SELECT e.wait_class,
       SUM(e.waits-b.waits) AS waits,
       SUM(e.wt-b.wt)       AS wait_us
FROM e JOIN b USING (wait_class, event)
WHERE upper(e.wait_class) NOT IN (''STATUS'',''NONE'')
GROUP BY e.wait_class
HAVING SUM(e.wt-b.wt) > 0
ORDER BY wait_us DESC LIMIT {{top}};
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false},{"key":"top","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.wdr_enabled', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SHOW enable_wdr_snapshot;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.window', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT to_char(b.start_ts,''YYYY-MM-DD HH24:MI'') AS b_start,
       to_char(e.start_ts,''YYYY-MM-DD HH24:MI'') AS e_start,
       round(EXTRACT(EPOCH FROM (e.start_ts-b.start_ts))/60)::bigint AS dur
FROM (SELECT start_ts FROM snapshot.snapshot WHERE snapshot_id={{begin}}) b,
     (SELECT start_ts FROM snapshot.snapshot WHERE snapshot_id={{end}}) e;
', '[{"key":"begin","value":"","type":"INTEGER","autoAcquire":false},{"key":"end","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.chain', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT w.sessionid       AS sessionid,
       w.block_sessionid AS block_sessionid
  FROM pg_thread_wait_status w
 WHERE w.block_sessionid IS NOT NULL
   AND w.block_sessionid <> 0
   AND w.block_sessionid <> w.sessionid;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.pairs', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT w.pid                         AS waiter_pid,
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.autovac_settings', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name, setting
  FROM pg_settings
 WHERE name LIKE ''autovacuum%'' OR name LIKE ''vacuum_cost%''
 ORDER BY name;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.autovac_workers', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT pid,
       COALESCE(sessionid,0) AS sessionid,
       EXTRACT(EPOCH FROM (now()-xact_start)) AS xact_age_s,
       COALESCE(query,'''') AS query
  FROM pg_stat_activity
 WHERE query LIKE ''autovacuum:%'';
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.dead_tuples', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname AS schema,
       t.relname AS "table",
       t.n_live_tup AS n_live_tup,
       t.n_dead_tup AS n_dead_tup,
       c.reltuples AS reltuples,
       pg_total_relation_size(c.oid) AS table_bytes,
       EXTRACT(EPOCH FROM (now()-t.last_autovacuum)) AS last_autovacuum_age_s,
       EXTRACT(EPOCH FROM (now()-t.last_vacuum)) AS last_vacuum_age_s,
       t.vacuum_count AS vacuum_count,
       t.autovacuum_count AS autovacuum_count,
       CASE WHEN ''autovacuum_enabled=false'' = ANY(c.reloptions) THEN false ELSE true END AS autovac_enabled,
       COALESCE(array_to_string(c.reloptions, '',''), '''') AS reloptions
  FROM pg_stat_user_tables t
  JOIN pg_class c ON c.oid = t.relid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',''dbe_pldeveloper'',''cstore'')
 ORDER BY t.n_dead_tup DESC
 LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'waitevent.events', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_type AS wait_class, snap_event AS event, sum(snap_wait) AS waits, sum(snap_total_wait_time) AS wt
             FROM snapshot.snap_global_wait_events WHERE snapshot_id={{b}} GROUP BY snap_type, snap_event),
     e AS (SELECT snap_type AS wait_class, snap_event AS event, sum(snap_wait) AS waits, sum(snap_total_wait_time) AS wt
             FROM snapshot.snap_global_wait_events WHERE snapshot_id={{e}} GROUP BY snap_type, snap_event)
SELECT e.wait_class,
       e.event,
       SUM(e.waits-b.waits) AS waits,
       SUM(e.wt-b.wt)       AS wait_us
FROM e JOIN b USING (wait_class, event)
WHERE upper(e.wait_class) NOT IN (''STATUS'',''NONE'')
GROUP BY e.wait_class, e.event
HAVING SUM(e.wt-b.wt) > 0
ORDER BY wait_us DESC LIMIT {{top}};
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false},{"key":"top","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'waitevent.instance_time', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH b AS (SELECT snap_stat_name AS stat_name, sum(snap_value) AS v
             FROM snapshot.snap_global_instance_time
            WHERE snapshot_id = {{b}} GROUP BY snap_stat_name),
     e AS (SELECT snap_stat_name AS stat_name, sum(snap_value) AS v
             FROM snapshot.snap_global_instance_time
            WHERE snapshot_id = {{e}} GROUP BY snap_stat_name)
SELECT e.stat_name AS stat_name,
       (e.v - b.v)  AS delta_us
  FROM e JOIN b USING (stat_name)
 ORDER BY delta_us DESC;
', '[{"key":"b","value":"","type":"INTEGER","autoAcquire":false},{"key":"e","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.kernel_funcs', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''version'' AS item, version() AS detail
UNION ALL
SELECT ''func:'' || p.proname AS item, pg_get_function_arguments(p.oid) AS detail
  FROM pg_proc p
 WHERE p.proname IN (''gs_get_explain'', ''gs_get_kernel_info'');
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.active_pid', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT pid, unique_sql_id, query, query_start
  FROM pg_stat_activity
 WHERE unique_sql_id = {{sql_id}}
   AND state = ''active''
   AND pid <> pg_backend_pid()
 ORDER BY query_start
 LIMIT 1;
', '[{"key":"sql_id","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.kernel_info', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT node_name, module, name, value
  FROM gs_get_kernel_info();
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.runtime_plan', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT gs_get_explain({{pid}}::bigint) AS plan;
', '[{"key":"pid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.session_by_pid', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT pid, unique_sql_id, query, query_start, state
  FROM pg_stat_activity
 WHERE pid = {{pid}}
 LIMIT 1;
', '[{"key":"pid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.runtime_plan_int4', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT gs_get_explain({{pid}}::integer) AS plan;
', '[{"key":"pid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.multi_stmt_probe', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO public; SELECT 1 AS ok;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.plan_text_schema', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.plan_text_analyze_schema', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.plan_text_schema', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.plan_text_analyze_schema', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.plan_json_schema', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT JSON) {{sql}}
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.plan_text_schema', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.plan_text_analyze_schema', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SET search_path TO "{{schema}}", public; EXPLAIN (ANALYZE false, BUFFERS false, FORMAT TEXT) {{sql}}
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.from_statement', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT query, user_name FROM dbe_perf.statement
WHERE unique_sql_id = {{sid}}
  AND query IS NOT NULL
  AND LENGTH(TRIM(query)) > 0
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqlfetch.from_statement', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT query, user_name FROM dbe_perf.statement
WHERE unique_sql_id = {{sid}}
  AND query IS NOT NULL
  AND LENGTH(TRIM(query)) > 0
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqlreview.from_statement', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT query, user_name FROM dbe_perf.statement
WHERE unique_sql_id = {{sid}}
  AND query IS NOT NULL
  AND LENGTH(TRIM(query)) > 0
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.sql_from_statement', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT query, user_name FROM dbe_perf.statement
WHERE unique_sql_id = {{sid}}
  AND query IS NOT NULL
  AND LENGTH(TRIM(query)) > 0
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.proc_def', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, p.proname, l.lanname, p.prosrc,
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
', '[{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'procinfo.proc_def', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, p.proname, l.lanname, p.prosrc,
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
', '[{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"schema","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.indexes', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname AS schema_name, t.relname AS table_name,
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
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.indexes', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname AS schema_name, t.relname AS table_name, i.relname AS index_name,
       ix.indisunique, ix.indisprimary,
       pg_get_indexdef(ix.indexrelid) AS index_def
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname IN ({{names}});
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.column_stats', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schemaname, tablename, attname, n_distinct, null_frac, avg_width, correlation,
       COALESCE(most_common_vals::text, '''') AS most_common_vals,
       COALESCE(most_common_freqs::text, '''') AS most_common_freqs,
       COALESCE(histogram_bounds::text, '''') AS histogram_bounds
FROM pg_stats
WHERE tablename IN ({{names}});
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.column_stats', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schemaname, tablename, attname, n_distinct, null_frac, avg_width, correlation,
       COALESCE(most_common_vals::text, '''') AS most_common_vals,
       COALESCE(most_common_freqs::text, '''') AS most_common_freqs,
       COALESCE(histogram_bounds::text, '''') AS histogram_bounds
FROM pg_stats
WHERE tablename IN ({{names}});
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.column_types', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname AS schema_name, c.relname AS table_name,
       a.attname, format_type(a.atttypid, NULL) AS type_name
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname IN ({{tables}})
  AND a.attname IN ({{columns}})
  AND a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN (''r'',''v'',''p'',''m'')
  AND n.nspname NOT IN (''pg_catalog'',''information_schema'');
', '[{"key":"tables","value":"","type":"STRING","autoAcquire":false},{"key":"columns","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'procinfo.locate_context', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT current_database() AS db, current_user AS usr,
       (SELECT string_agg(datname, '', '' ORDER BY datname) FROM pg_database WHERE NOT datistemplate) AS databases;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'procinfo.locate_schema', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, pg_catalog.has_schema_privilege(n.oid, ''USAGE'') AS usage
FROM pg_namespace n
WHERE n.nspname = ''{{schema}}'';
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'procinfo.locate_search', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''proc'' AS kind, n.nspname, COALESCE(k.pkgname, '''') AS pkgname, p.proname
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
', '[{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.locate_context', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT current_database() AS db, current_user AS usr,
       (SELECT string_agg(datname, '', '' ORDER BY datname) FROM pg_database WHERE NOT datistemplate) AS databases;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.locate_schema', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, pg_catalog.has_schema_privilege(n.oid, ''USAGE'') AS usage
FROM pg_namespace n
WHERE n.nspname = ''{{schema}}'';
', '[{"key":"schema","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.locate_search', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''proc'' AS kind, n.nspname, COALESCE(k.pkgname, '''') AS pkgname, p.proname
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
', '[{"key":"name","value":"","type":"STRING","autoAcquire":false},{"key":"package","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.from_history', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema_name, query
FROM dbe_perf.statement_history
WHERE unique_query_id = {{sid}}
  AND query NOT LIKE ''/* missing SQL statement%''
ORDER BY start_time DESC
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.from_statement', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT query, user_name FROM dbe_perf.statement
WHERE unique_sql_id = {{sid}}
  AND query IS NOT NULL
  AND LENGTH(TRIM(query)) > 0
LIMIT 1;
', '[{"key":"sid","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.relation_schemas', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname, c.relname
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'',''f'');
', '[{"key":"names","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'config.settings', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name,
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
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.partitions', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname      AS schema,
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
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.storage', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''database'' AS kind,
       d.datname  AS name,
       pg_database_size(d.oid)::bigint AS bytes
  FROM pg_database d
 WHERE d.datallowconn
UNION ALL
SELECT ''tablespace'' AS kind,
       t.spcname    AS name,
       pg_tablespace_size(t.oid)::bigint AS bytes
  FROM pg_tablespace t
 ORDER BY bytes DESC;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.sync_commit', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''peers_configured'' AS item,
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
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.thread_pool', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''enable_thread_pool'' AS item,
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
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.wal_slots', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT slot_name,
       COALESCE(slot_type,'''')  AS slot_type,
       COALESCE(database,'''')   AS database,
       active::text            AS active,
       pg_xlog_location_diff(pg_current_xlog_location(), restart_lsn)::bigint AS lag_bytes
  FROM pg_replication_slots
 WHERE restart_lsn IS NOT NULL
 ORDER BY lag_bytes DESC;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.bloat_estimate', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT n.nspname AS schema,
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.freeze_age', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT d.datname,
       (txid_snapshot_xmin(txid_current_snapshot())
        - d.datfrozenxid64::text::numeric)::bigint      AS freeze_age,
       (SELECT setting::numeric FROM pg_settings
         WHERE name = ''autovacuum_freeze_max_age'')::bigint AS max_age,
       (SELECT setting FROM pg_settings
         WHERE name = ''vacuum_freeze_table_age'')         AS freeze_table_age
  FROM pg_database d
 WHERE d.datallowconn
 ORDER BY freeze_age DESC;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.snap_counters', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''snap_at'' AS item,
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
', '[{"key":"snap","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.live_counters', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''now'' AS item, to_char(now(), ''YYYY-MM-DD HH24:MI:SS'') AS value
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
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.snap_waits', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT snap_type  AS wait_class,
       snap_event AS event,
       sum(snap_wait)            AS waits,
       sum(snap_total_wait_time) AS wait_us
  FROM snapshot.snap_global_wait_events
 WHERE snapshot_id = {{snap}}
   AND upper(snap_type) NOT IN (''STATUS'',''NONE'')
 GROUP BY snap_type, snap_event
HAVING sum(snap_wait) > 0;
', '[{"key":"snap","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.live_waits', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT type  AS wait_class,
       event AS event,
       sum(wait)            AS waits,
       sum(total_wait_time) AS wait_us
  FROM dbe_perf.global_wait_events
 WHERE upper(type) NOT IN (''STATUS'',''NONE'')
 GROUP BY type, event
HAVING sum(wait) > 0;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'explain.plan_from_history', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT schema_name,
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
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.plan_in_window', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT to_char(start_time, ''YYYY-MM-DD HH24:MI:SS'') AS start_time,
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
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.long_xact', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT pid,
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
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.conn_concentration', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT COALESCE(query,'''') q, count(*) c, sum(count(*)) OVER () AS total
FROM pg_stat_activity
WHERE state=''active'' AND COALESCE(query,'''')<>'''' AND client_port IS NOT NULL
GROUP BY query ORDER BY c DESC LIMIT 1;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.conn_states', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT COALESCE(state,''<null>'') AS state, count(*) AS cnt
FROM pg_stat_activity
WHERE sessionid <> 0
GROUP BY state
ORDER BY cnt DESC;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.stale_stats', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT tbl_name, frozen_pages, cur_pages, frozen_tuples, live_tuples,
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
', '[{"key":"min_rows","value":"","type":"INTEGER","autoAcquire":false},{"key":"min_pages","value":"","type":"INTEGER","autoAcquire":false},{"key":"schema_filter","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'vacuum.oldest_xmin', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT ''long_xact'' AS source,
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
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.asp_status', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name AS item, setting AS value
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
', '[{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"end_time","value":"","type":"DATETIME","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.asp_pairs', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH t AS (
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
', '[{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"end_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'lockwait.asp_sessions', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'WITH t AS (
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
', '[{"key":"begin_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"end_time","value":"","type":"DATETIME","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.gucs', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT name, setting
FROM pg_settings
WHERE name IN (
  ''max_process_memory'', ''shared_buffers'', ''work_mem'', ''maintenance_work_mem'',
  ''max_connections'', ''enable_memory_limit'', ''memory_tracking_mode'',
  ''use_workload_manager'', ''enable_resource_track'', ''resource_track_level'',
  ''resource_track_cost'', ''resource_track_duration'', ''enable_resource_record'',
  ''enable_dynamic_workload'', ''query_max_mem'', ''query_mem'')
ORDER BY name;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'topsql.top_sql', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT
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
', '[{"key":"order","value":"","type":"STRING","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'topsql.snap_statement', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT
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
', '[{"key":"snap","value":"","type":"INTEGER","autoAcquire":false},{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'health.overview', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT
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
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.stat_now', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT sum(n_calls) AS calls,
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
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.stat_trend', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT s.snapshot_id,
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
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.exec_history', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT hour, runs, avg_ms, max_ms, plan_sig,
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
