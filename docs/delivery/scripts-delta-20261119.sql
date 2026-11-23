-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-19）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 132 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 132 条）。
--
-- 升级路径：本文件用于从 v1.4.14 升级到 v1.5.0（更新 4 条：拼接改用 concat，修 M 兼容业务库上报错）。
-- 更早的版本：先按 v1.4.14 的说明依次导完以前的增量（最后一份是 scripts-delta-20261116.sql），再导本文件。
-- 不确定导到了哪一份时，把白名单导出交给我方核对。
--
--   新增   0 条 INSERT
--   更新   4 条 UPDATE
--
-- 全部脚本均为**只读查询**，只访问系统视图与系统表，不读业务数据、
-- 不含 DML/DDL、不修改任何对象。
--
-- 执行库（database_type）：appbusiness 的脚本 parameter_config 最前面带
--   {"key":"dbName","value":"","type":"String","autoAcquire":false}，
-- 中间件按调用时传入的 dbName 切到该业务库执行；postgres 的脚本不带 dbName，在 postgres 库执行。
-- 本文件里的 UPDATE 只改脚本正文(script_content),不动执行库(database_type)与参数定义(parameter_config)。
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
-- 二、更新 4 条已有脚本（SQL 正文 / 参数定义 / 执行库）
-- =====================================================================

-- [explain.kernel_funcs]
--   **函数名一列的拼接改用 concat**:concat('func:', p.proname)。本脚本在业务库执行,业务库可能是 M 兼容模式,那里 || 是逻辑或、不是字符串拼接,整条报 「argument of OR must be type boolean, not type name」(2026-11-17 现场反馈)。concat 在 A / M 两种兼容模式下都是拼接, A 兼容库上结果与原写法一致。列名、筛选不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''version'' AS item, version() AS detail
UNION ALL
SELECT concat(''func:'', p.proname) AS item, pg_get_function_arguments(p.oid) AS detail
  FROM pg_proc p
 WHERE p.proname IN (''gs_get_explain'', ''gs_get_kernel_info'');
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.kernel_funcs';

-- [health.stale_stats]
--   **表名一列的拼接改用 concat**:concat(n.nspname, '.', c.relname)。本脚本在业务库执行,业务库可能是 M 兼容模式,那里 || 是逻辑或、不是字符串拼接,整条报 「argument of OR must be type boolean, not type name」(2026-11-17 现场反馈)。concat 在 A / M 两种兼容模式下都是拼接, A 兼容库上结果与原写法一致。列名、筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT tbl_name, frozen_pages, cur_pages, frozen_tuples, live_tuples,
       stat_columns, last_analyze, last_autoanalyze
FROM (
  SELECT concat(n.nspname, ''.'', c.relname) AS tbl_name,
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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.stale_stats';

-- [health.unused_index]
--   **索引名一列的拼接改用 concat**:concat(s.schemaname, '.', s.indexrelname)。本脚本在业务库执行,业务库可能是 M 兼容模式,那里 || 是逻辑或、不是字符串拼接,整条报 「argument of OR must be type boolean, not type name」(2026-11-17 现场反馈)。concat 在 A / M 两种兼容模式下都是拼接, A 兼容库上结果与原写法一致。列名、筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT concat(s.schemaname, ''.'', s.indexrelname) AS idx_name,
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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.unused_index';

-- [lockwait.pairs]
--   **锁对象一列的拼接改用 concat**:CASE WHEN c.relname IS NULL THEN '' ELSE concat(n.nspname, '.', c.relname) END。本脚本在业务库执行,业务库可能是 M 兼容模式,那里 || 是逻辑或、不是字符串拼接,整条报 「argument of OR must be type boolean, not type name」(2026-11-17 现场反馈)。concat 在 A / M 两种兼容模式下都是拼接, A 兼容库上结果与原写法一致。 锁对象不是表(事务号锁等)时照旧返回空串。列名、筛选、排序不变;时间差写法同 v1.4.14。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT w.pid                         AS waiter_pid,
       COALESCE(w.sessionid, 0)      AS waiter_sessionid,
       w.mode                        AS waiter_mode,
       h.pid                         AS holder_pid,
       COALESCE(h.sessionid, 0)      AS holder_sessionid,
       h.mode                        AS holder_mode,
       w.locktype                    AS locktype,
       CASE WHEN c.relname IS NULL THEN '''' ELSE concat(n.nspname, ''.'', c.relname) END AS lock_object,
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

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 132：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 本次没有新增脚本。
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('explain.kernel_funcs', '896b5c1753ab8fca536ac67ac852d9bb'),
--     ('health.stale_stats', '5b78f231bf8267ec44f210a2a0b207c9'),
--     ('health.unused_index', '23e31f4263e706cc4c7595262febfd68'),
--     ('lockwait.pairs', 'da54f420c37011ad2050cf565e7472e2')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
-- 执行库应与本版一致（appbusiness 应为 39 条，且都以 dbName 开头）：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness';
--   SELECT script_name FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness'
--      AND parameter_config NOT LIKE '[{"key":"dbName",%';     -- 应返回 0 行
