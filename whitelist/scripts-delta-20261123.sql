-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-23）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 132 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 132 条）。
--
-- 从 v1.5.0 升级到 v1.5.1;更早的版本先按顺序导到 scripts-delta-20261119.sql,再导这一份
--
--   新增   0 条 INSERT
--   更新   13 条 UPDATE
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
-- 二、更新 13 条已有脚本（SQL 正文 / 参数定义 / 执行库）
-- =====================================================================

-- [explain.kernel_funcs]
--   函数名一列 concat 里的名字列先 ::text(M 兼容库的类型推导)。列名、筛选不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''version'' AS item, version() AS detail
UNION ALL
SELECT concat(''func:'', p.proname::text) AS item, pg_get_function_arguments(p.oid) AS detail
  FROM pg_proc p
 WHERE p.proname IN (''gs_get_explain'', ''gs_get_kernel_info'');
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.kernel_funcs';

-- [health.partitions]
--   **分区策略一列不再直接聚合 "char" 列**:先用 CASE 把 partstrategy 映射成同样的单字母文字,再取 max。本脚本在业务库执行,M 兼容业务库上 max() / COALESCE() 处理 "char" 类型报「un support type」(2026-11-20 现场反馈) 。输出列与取值不变(r / i / l / a / h / n),A 兼容库上结果与原写法一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname AS schema,
       c.relname AS "table",
       count(*)  AS parts,
       COALESCE(max(CASE p.partstrategy WHEN ''r'' THEN ''r'' WHEN ''i'' THEN ''i'' WHEN ''l'' THEN ''l''
                                        WHEN ''a'' THEN ''a'' WHEN ''h'' THEN ''h'' WHEN ''n'' THEN ''n''
                                        ELSE ''?'' END), '''') AS strategy
  FROM pg_partition p
  JOIN pg_class c     ON c.oid = p.parentid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE p.parttype = ''p''
   AND n.nspname NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',
                         ''dbe_pldeveloper'',''cstore'',''pg_toast'')
 GROUP BY n.nspname, c.relname
 ORDER BY parts DESC;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.partitions';

-- [health.stale_stats]
--   **偏离比例不再用 CAST(… AS float8)**,改为 * 1.0 / 求小数:M 兼容库的 CAST 目标类型里没有 float8。表名一列 concat 里的名字列先 ::text(M 兼容库按自己的类型推导处理 name 类型) 。列名、筛选、排序不变,A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT tbl_name, frozen_pages, cur_pages, frozen_tuples, live_tuples,
       stat_columns, last_analyze, last_autoanalyze
FROM (
  SELECT concat(n.nspname::text, ''.'', c.relname::text) AS tbl_name,
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
              THEN abs(cur_pages - frozen_pages) * 1.0 / frozen_pages
              ELSE 0 END DESC,
         cur_pages DESC
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.stale_stats';

-- [health.table_writes]
--   **改成一层查询**:原写法在 M 兼容业务库上报「operator does not exist: any <> unknown」(两个 name 类型取 COALESCE 后类型推导成 any,2026-11-20 现场反馈) ;外层引用的 "table" 在 M 库又是字符串常量,修好前一处后整列会变成字面值 table。现在名字列先转成文字再取,双引号只用作最外层列别名。口径不变:普通表取自身、大字段(TOAST)写入折回所属表、找不到所属表的 TOAST 与系统 schema 排除; 列名、排序不变,A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT CASE WHEN s.schemaname = ''pg_toast'' THEN pn.nspname::text ELSE s.schemaname::text END AS schema,
       CASE WHEN s.schemaname = ''pg_toast'' THEN pc.relname::text ELSE s.relname::text END AS "table",
       (s.schemaname = ''pg_toast'') AS is_toast,
       s.n_tup_ins, s.n_tup_upd, s.n_tup_del,
       CASE WHEN s.schemaname = ''pg_toast'' THEN pg_relation_size(s.relid) END AS toast_bytes
  FROM pg_stat_all_tables s
  LEFT JOIN pg_class pc     ON s.schemaname = ''pg_toast'' AND pc.reltoastrelid = s.relid
  LEFT JOIN pg_namespace pn ON pn.oid = pc.relnamespace
 WHERE s.n_tup_ins + s.n_tup_upd + s.n_tup_del > 0
   AND ((s.schemaname <> ''pg_toast''
         AND s.schemaname NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',''dbe_pldeveloper'',''cstore''))
     OR (s.schemaname = ''pg_toast'' AND pn.nspname IS NOT NULL
         AND pn.nspname NOT IN (''pg_catalog'',''information_schema'',''snapshot'',''dbe_perf'',''dbe_pldeveloper'',''cstore'',''pg_toast'')))
 ORDER BY s.last_data_changed DESC NULLS LAST
 LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.table_writes';

-- [health.unused_index]
--   索引名一列 concat 里的名字列先 ::text(M 兼容库按自己的类型推导处理 name 类型,显式转成文字两种模式一致)。列名、筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT concat(s.schemaname::text, ''.'', s.indexrelname::text) AS idx_name,
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
--   锁对象一列 concat 里的名字列、等待方 / 持有方账号两列先 ::text(M 兼容库按自己的类型推导处理 name 类型)。列名、筛选、排序不变;锁对象不是表时照旧返回空串。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT w.pid                         AS waiter_pid,
       COALESCE(w.sessionid, 0)      AS waiter_sessionid,
       w.mode                        AS waiter_mode,
       h.pid                         AS holder_pid,
       COALESCE(h.sessionid, 0)      AS holder_sessionid,
       h.mode                        AS holder_mode,
       w.locktype                    AS locktype,
       CASE WHEN c.relname IS NULL THEN '''' ELSE concat(n.nspname::text, ''.'', c.relname::text) END AS lock_object,
       COALESCE(w.locktag, '''')       AS locktag,
       round(date_part(''epoch'', clock_timestamp() - wa.query_start)::numeric, 1) AS waiter_wait_s,
       COALESCE(wa.usename::text, '''') AS waiter_user,
       COALESCE(wa.application_name, '''') AS waiter_app,
       COALESCE(substr(wa.query, 1, 300), '''')       AS waiter_query,
       COALESCE(ha.state, '''')        AS holder_state,
       COALESCE(ha.usename::text, '''') AS holder_user,
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

-- [login.current_db]
--   **多取两列**:compat(本库的兼容模式,A / M …)、lctn(M 兼容库的表名大小写参数 lower_case_table_names,A 兼容库没有、返回空)。技能据此在 M 兼容库上按 MySQL 的写法处理(M 库没有 ADM_SOURCE、改写语句用反引号等) 。db / usr 两列不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT current_database() AS db, current_user AS usr,
       (SELECT datcompatibility::text FROM pg_database WHERE datname = current_database()) AS compat,
       (SELECT setting FROM pg_settings WHERE name = ''lower_case_table_names'') AS lctn;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'login.current_db';

-- [procinfo.locate_search]
--   **ILIKE 改成 lower(…) LIKE lower(…)**:M 兼容库不支持 ILIKE(2026-11-20 现场反馈:syntax error at or near "ILIKE")。两种写法都不分大小写; 名字列先转成文字(UNION 两个分支的列类型一致)。列名、排序、条数上限不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''proc'' AS kind, n.nspname::text AS nspname, COALESCE(k.pkgname::text, '''') AS pkgname, p.proname::text AS proname
FROM pg_proc p
LEFT JOIN pg_namespace n ON n.oid = p.pronamespace
LEFT JOIN gs_package k ON k.oid = p.propackageid
WHERE lower(p.proname::text) LIKE lower(''%{{name}}%'')
  AND (n.nspname IS NULL OR (n.nspname NOT LIKE ''pg_%'' AND n.nspname NOT LIKE ''dbe_%''
       AND n.nspname NOT IN (''information_schema'', ''pkg_service'', ''pkg_util'', ''sqladvisor'', ''db4ai'',
                             ''snapshot'', ''blockchain'', ''cstore'', ''pmk'', ''coverage'')))
UNION ALL
SELECT ''package'' AS kind, n.nspname::text AS nspname, k.pkgname::text AS pkgname, '''' AS proname
FROM gs_package k
LEFT JOIN pg_namespace n ON n.oid = k.pkgnamespace
WHERE COALESCE(length(''{{package}}''), 0) > 0 AND lower(k.pkgname::text) LIKE lower(''%{{package}}%'')
ORDER BY 1, 2, 3, 4
LIMIT 40;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'procinfo.locate_search';

-- [procinfo.proc_def]
--   包名一列 COALESCE 里的名字列先 ::text(M 兼容库的类型推导)。列名、筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, p.proname, l.lanname, p.prosrc,
       pg_catalog.pg_get_function_arguments(p.oid) AS args,
       COALESCE(k.pkgname::text, '''') AS package
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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'procinfo.proc_def';

-- [proctune.locate_search]
--   **ILIKE 改成 lower(…) LIKE lower(…)**,同 procinfo.locate_search(2026-11-20 现场反馈)。列名、排序、条数上限不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''proc'' AS kind, n.nspname::text AS nspname, COALESCE(k.pkgname::text, '''') AS pkgname, p.proname::text AS proname
FROM pg_proc p
LEFT JOIN pg_namespace n ON n.oid = p.pronamespace
LEFT JOIN gs_package k ON k.oid = p.propackageid
WHERE lower(p.proname::text) LIKE lower(''%{{name}}%'')
  AND (n.nspname IS NULL OR (n.nspname NOT LIKE ''pg_%'' AND n.nspname NOT LIKE ''dbe_%''
       AND n.nspname NOT IN (''information_schema'', ''pkg_service'', ''pkg_util'', ''sqladvisor'', ''db4ai'',
                             ''snapshot'', ''blockchain'', ''cstore'', ''pmk'', ''coverage'')))
UNION ALL
SELECT ''package'' AS kind, n.nspname::text AS nspname, k.pkgname::text AS pkgname, '''' AS proname
FROM gs_package k
LEFT JOIN pg_namespace n ON n.oid = k.pkgnamespace
WHERE COALESCE(length(''{{package}}''), 0) > 0 AND lower(k.pkgname::text) LIKE lower(''%{{package}}%'')
ORDER BY 1, 2, 3, 4
LIMIT 40;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.locate_search';

-- [proctune.proc_def]
--   包名一列 COALESCE 里的名字列先 ::text(M 兼容库的类型推导)。列名、筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname, p.proname, l.lanname, p.prosrc,
       pg_catalog.pg_get_function_arguments(p.oid) AS args,
       COALESCE(k.pkgname::text, '''') AS package
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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.proc_def';

-- [sqlreview.indexes]
--   **表名一列的别名改成 AS "table"**(同 sqlreview.tables,2026-11-20 现场反馈);索引列的顺序改用 position(… IN …) 在 indkey 的文本形态里定位,不再用数组下标与 generate_series。 表达式列照旧不列;列名、排序不变,A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  n.nspname::text                                              AS schema,
  t.relname::text                                              AS "table",
  i.relname::text                                              AS name,
  COALESCE((SELECT array_to_string(array_agg(a.attname::text
                     ORDER BY position(concat('' '', a.attnum::text, '' '') IN concat('' '', ix.indkey::text, '' ''))), '','')
              FROM pg_attribute a
             WHERE a.attrelid = t.oid AND a.attnum > 0
               AND position(concat('' '', a.attnum::text, '' '') IN concat('' '', ix.indkey::text, '' '')) > 0), '''') AS columns,
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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqlreview.indexes';

-- [sqlreview.tables]
--   **表名一列的别名改成 AS "table"**:TABLE 在 M 兼容库是保留字,原写法 AS table 报「syntax error at or near "table"」(2026-11-20 现场反馈)。顺带去掉 ARRAY[]: :text[] 字面量(空集仍返回空串)。列名、筛选、排序不变,A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  n.nspname::text                                              AS schema,
  c.relname::text                                              AS "table",
  EXISTS (SELECT 1 FROM pg_constraint pk
          WHERE pk.conrelid = c.oid AND pk.contype = ''p'')      AS has_pk,
  COALESCE((SELECT array_to_string(array_agg(fk.conname::text), '','')
              FROM pg_constraint fk
             WHERE fk.conrelid = c.oid AND fk.contype = ''f''), '''') AS fks,
  COALESCE((SELECT array_to_string(array_agg(a.attname::text ORDER BY a.attnum), '','')
              FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), '''') AS columns
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = ''r'' AND n.nspname = ''{{schema}}''
ORDER BY c.relname;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqlreview.tables';

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 132：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 本次没有新增脚本。
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('explain.kernel_funcs', '46bab9604c9c4d0e4e4845d23bc9a0aa'),
--     ('health.partitions', '61bfcb4af10cd4bb434a428b7c98f0be'),
--     ('health.stale_stats', 'fe3902c3a95cffa30811cd967bc753eb'),
--     ('health.table_writes', 'ad002bf7af92662ef3380518029c2ca9'),
--     ('health.unused_index', 'b206bdd76c3ac3fe31caed2fd289bdc2'),
--     ('lockwait.pairs', '52cfd536e1f5b7564039cb04bbf9c41f'),
--     ('login.current_db', '365d9bd8f934527ff3a53503d9052fd4'),
--     ('procinfo.locate_search', 'cd2f6244e75792b318feeaae834bc7bc'),
--     ('procinfo.proc_def', '4444490135788fde265602966eee3376'),
--     ('proctune.locate_search', 'cd2f6244e75792b318feeaae834bc7bc'),
--     ('proctune.proc_def', '4444490135788fde265602966eee3376'),
--     ('sqlreview.indexes', '9f7610cea419d3dc88c13431228ab602'),
--     ('sqlreview.tables', '935be833d4ea78f9b2dd513277f91e3b')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
-- 执行库应与本版一致（appbusiness 应为 39 条，且都以 dbName 开头）：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness';
--   SELECT script_name FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness'
--      AND parameter_config NOT LIKE '[{"key":"dbName",%';     -- 应返回 0 行
