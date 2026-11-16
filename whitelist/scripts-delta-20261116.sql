-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-16）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 132 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 132 条）。
--
--   新增   0 条 INSERT
--   更新   2 条 UPDATE
--
-- 全部脚本均为**只读查询**，只访问系统视图与系统表，不读业务数据、
-- 不含 DML/DDL、不修改任何对象。
--
-- 执行库（database_type）：appbusiness 的脚本 parameter_config 最前面带
--   {"key":"dbName","value":"","type":"String","autoAcquire":false}，
-- 中间件按调用时传入的 dbName 切到该业务库执行；postgres 的脚本不带 dbName，在 postgres 库执行。
-- 本文件里的 UPDATE 会同时写 database_type 与 parameter_config，以本文件为准覆盖手工改过的记录。
--
-- 导入前请确认两处与现有规范的对齐：
--   1. `create_user` / `last_modify_user` 当前填 '999999999'，与上一版保持一致；
--      若要求填真实工号，请统一替换。
--   2. id 用 `grmp.script_config_seq.nextval`，不硬编码，避免与现有记录冲突。
--
-- =====================================================================
-- 一、新增 0 条
-- =====================================================================

-- =====================================================================
-- 二、更新 2 条已有脚本（SQL 正文 / 参数定义 / 执行库）
-- =====================================================================

-- [lockwait.pairs]
--   **时间差改用 date_part('epoch', clock_timestamp() - 列)**(waiter_wait_s、holder_xact_age_s 两列),原因同 vacuum.dead_tuples: 本脚本也在业务库执行,原写法在 M 兼容业务库上同样会报「un support type」。列名、筛选、排序不变。只读。
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
       round(date_part(''epoch'', clock_timestamp() - wa.query_start)::numeric, 1) AS waiter_wait_s,
       COALESCE(wa.usename, '''')      AS waiter_user,
       COALESCE(wa.application_name, '''') AS waiter_app,
       COALESCE(substr(wa.query, 1, 300), '''')       AS waiter_query,
       COALESCE(ha.state, '''')        AS holder_state,
       COALESCE(ha.usename, '''')      AS holder_user,
       COALESCE(ha.application_name, '''') AS holder_app,
       round(date_part(''epoch'', clock_timestamp() - ha.xact_start)::numeric, 1) AS holder_xact_age_s,
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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'lockwait.pairs';

-- [vacuum.dead_tuples]
--   **多取三列**：这张表的更新、HOT 更新、删除次数（pg_stat_user_tables 的 n_tup_upd / n_tup_hot_upd / n_tup_del）， 报告据此说明死元组来自哪类业务操作。筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname AS schema,
       t.relname AS "table",
       t.n_live_tup AS n_live_tup,
       t.n_dead_tup AS n_dead_tup,
       c.reltuples AS reltuples,
       pg_total_relation_size(c.oid) AS table_bytes,
       date_part(''epoch'', clock_timestamp() - t.last_autovacuum) AS last_autovacuum_age_s,
       date_part(''epoch'', clock_timestamp() - t.last_vacuum) AS last_vacuum_age_s,
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
-- 应为 132：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 本次没有新增脚本。
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('lockwait.pairs', 'b21c94bd2fcc30a62a9eba91e7621522'),
--     ('vacuum.dead_tuples', 'fd79553b7fafacb360b2bbd8a40f0f84')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
-- 执行库应与本版一致（appbusiness 应为 39 条，且都以 dbName 开头）：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness';
--   SELECT script_name FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness'
--      AND parameter_config NOT LIKE '[{"key":"dbName",%';     -- 应返回 0 行
