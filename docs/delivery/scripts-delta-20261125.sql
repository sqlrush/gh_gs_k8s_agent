-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-25）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 132 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 133 条）。
--
-- 从 v1.5.1 升级到 v1.5.2;更早的版本先按顺序导到 scripts-delta-20261123.sql,再导这一份
--
--   新增   1 条 INSERT
--   更新   21 条 UPDATE
--
-- 全部脚本均为**只读查询**，只访问系统视图与系统表，不读业务数据、
-- 不含 DML/DDL、不修改任何对象。
--
-- 执行库（database_type）：appbusiness 的脚本 parameter_config 最前面带
--   {"key":"dbName","value":"","type":"String","autoAcquire":false}，
-- 中间件按调用时传入的 dbName 切到该业务库执行；postgres 的脚本不带 dbName，在 postgres 库执行。
-- 本文件里的 INSERT 带完整的执行库(database_type)与参数定义(parameter_config)。
-- 本文件里的 UPDATE 只改脚本正文(script_content),不动执行库(database_type)与参数定义(parameter_config)。
--
-- 导入前请确认两处与现有规范的对齐：
--   1. `create_user` / `last_modify_user` 当前填 '999999999'，与上一版保持一致；
--      若要求填真实工号，请统一替换。
--   2. id 用 `grmp.script_config_seq.nextval`，不硬编码，避免与现有记录冲突。
--
-- =====================================================================
-- 一、新增 1 条
-- =====================================================================

-- [sqltune.plan_yaml] 取一条只读 SQL 的执行计划（YAML 格式，M 兼容业务库上代价推演用）
--   读取：EXPLAIN (ANALYZE false, BUFFERS false, FORMAT YAML) <用户 SQL>（不执行该 SQL，只出估算计划）
--   用途：与 sqltune.plan_json 内容相同、格式不同:M 兼容业务库上 JSON 计划的列(json 类型)经中间件取不到原值,YAML 计划的列是文字类型;只在 JSON 计划取不到时调用。业务库类,带 dbName。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'sqltune.plan_yaml', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'EXPLAIN (ANALYZE false, BUFFERS false, FORMAT YAML) {{sql}}
', '[{"key":"dbName","value":"","type":"String","autoAcquire":false},{"key":"sql","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- =====================================================================
-- 二、更新 21 条已有脚本（SQL 正文 / 参数定义 / 执行库）
-- =====================================================================

-- [explain.relation_schemas]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, c.relname::text AS relname
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'',''f'');
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'explain.relation_schemas';

-- [health.partitions]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS schema,
       c.relname::text AS "table",
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

-- [health.table_writes]
--   **是否 TOAST 一列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END):业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT CASE WHEN s.schemaname = ''pg_toast'' THEN pn.nspname::text ELSE s.schemaname::text END AS schema,
       CASE WHEN s.schemaname = ''pg_toast'' THEN pc.relname::text ELSE s.relname::text END AS "table",
       CASE WHEN s.schemaname = ''pg_toast'' THEN 1 ELSE 0 END AS is_toast,
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
--   **布尔列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END),名字类(name)列改为 ::text:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT concat(s.schemaname::text, ''.'', s.indexrelname::text) AS idx_name,
       s.relname::text AS table_name,
       s.idx_scan,
       s.idx_tup_read,
       s.idx_tup_fetch,
       pg_relation_size(s.indexrelid) AS idx_bytes,
       CASE WHEN i.indisprimary THEN 1 ELSE 0 END AS indisprimary,
       CASE WHEN i.indisunique THEN 1 ELSE 0 END AS indisunique,
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

-- [procinfo.locate_schema]
--   **布尔列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END),名字类(name)列改为 ::text:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, CASE WHEN pg_catalog.has_schema_privilege(n.oid, ''USAGE'') THEN 1 ELSE 0 END AS usage
FROM pg_namespace n
WHERE n.nspname = ''{{schema}}'';
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'procinfo.locate_schema';

-- [procinfo.proc_def]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, p.proname::text AS proname, l.lanname::text AS lanname, p.prosrc,
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

-- [proctune.column_stats]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT schemaname::text AS schemaname, tablename::text AS tablename, attname::text AS attname, n_distinct, null_frac, avg_width, correlation,
       COALESCE(most_common_vals::text, '''') AS most_common_vals,
       COALESCE(most_common_freqs::text, '''') AS most_common_freqs,
       COALESCE(histogram_bounds::text, '''') AS histogram_bounds
FROM pg_stats
WHERE tablename IN ({{names}});
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.column_stats';

-- [proctune.indexes]
--   **布尔列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END),名字类(name)列改为 ::text:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS schema_name, t.relname::text AS table_name, i.relname::text AS index_name,
       CASE WHEN ix.indisunique THEN 1 ELSE 0 END AS indisunique,
       CASE WHEN ix.indisprimary THEN 1 ELSE 0 END AS indisprimary,
       pg_get_indexdef(ix.indexrelid) AS index_def
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname IN ({{names}});
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.indexes';

-- [proctune.locate_schema]
--   **布尔列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END),名字类(name)列改为 ::text:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, CASE WHEN pg_catalog.has_schema_privilege(n.oid, ''USAGE'') THEN 1 ELSE 0 END AS usage
FROM pg_namespace n
WHERE n.nspname = ''{{schema}}'';
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.locate_schema';

-- [proctune.proc_def]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, p.proname::text AS proname, l.lanname::text AS lanname, p.prosrc,
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

-- [proctune.tables]
--   **relkind("char" 类型)与名字类列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, c.relname::text AS relname, c.relpages,
       c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) / current_setting(''block_size'')::bigint AS curpages,
       c.relkind::text AS relkind,
       pg_total_relation_size(c.oid) / 1024.0 / 1024.0 AS size_mb
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'');
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'proctune.tables';

-- [sqlreview.indexes]
--   **是否唯一 / 是否主键两列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END):业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
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
  CASE WHEN ix.indisunique THEN 1 ELSE 0 END                   AS is_unique,
  CASE WHEN ix.indisprimary THEN 1 ELSE 0 END                  AS is_primary,
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
--   **是否有主键一列改为输出 1/0**(CASE WHEN EXISTS … THEN 1 ELSE 0 END):业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  n.nspname::text                                              AS schema,
  c.relname::text                                              AS "table",
  CASE WHEN EXISTS (SELECT 1 FROM pg_constraint pk
                    WHERE pk.conrelid = c.oid AND pk.contype = ''p'')
       THEN 1 ELSE 0 END                                       AS has_pk,
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

-- [sqltune.column_stats]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT schemaname::text AS schemaname, tablename::text AS tablename, attname::text AS attname, n_distinct, null_frac, avg_width, correlation,
       COALESCE(most_common_vals::text, '''') AS most_common_vals,
       COALESCE(most_common_freqs::text, '''') AS most_common_freqs,
       COALESCE(histogram_bounds::text, '''') AS histogram_bounds
FROM pg_stats
WHERE tablename IN ({{names}});
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.column_stats';

-- [sqltune.column_types]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS schema_name, c.relname::text AS table_name,
       a.attname::text AS attname, format_type(a.atttypid, NULL) AS type_name
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname IN ({{tables}})
  AND a.attname IN ({{columns}})
  AND a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN (''r'',''v'',''p'',''m'')
  AND n.nspname NOT IN (''pg_catalog'',''information_schema'');
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.column_types';

-- [sqltune.indexes]
--   **布尔列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END),名字类(name)列改为 ::text:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS schema_name, t.relname::text AS table_name,
       i.relname::text AS index_name,
       CASE WHEN ix.indisunique THEN 1 ELSE 0 END AS indisunique,
       CASE WHEN ix.indisprimary THEN 1 ELSE 0 END AS indisprimary,
       i.relpages AS index_relpages,
       i.reltuples::bigint AS index_reltuples,
       pg_get_indexdef(ix.indexrelid) AS index_def
FROM pg_class t
JOIN pg_index ix ON t.oid = ix.indrelid
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE t.relname IN ({{names}});
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.indexes';

-- [sqltune.stats_freshness]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT schemaname::text AS schemaname, relname::text AS relname,
       n_live_tup, n_dead_tup,
       COALESCE(to_char(last_analyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_analyze,
       COALESCE(to_char(last_autoanalyze, ''YYYY-MM-DD HH24:MI:SS''), ''never'') AS last_autoanalyze,
       analyze_count, autoanalyze_count
FROM pg_stat_user_tables
WHERE relname IN ({{names}});
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.stats_freshness';

-- [sqltune.tables]
--   **relkind("char" 类型)与名字类列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, c.relname::text AS relname, c.relpages,
       c.reltuples::bigint AS reltuples,
       pg_relation_size(c.oid) / current_setting(''block_size'')::bigint AS curpages,
       c.relkind::text AS relkind,
       pg_total_relation_size(c.oid) / 1024.0 / 1024.0 AS size_mb
FROM pg_class c
LEFT JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE c.relname IN ({{names}}) AND c.relkind IN (''r'',''v'',''p'',''m'');
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'sqltune.tables';

-- [topproc.top_procs]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS nspname, p.proname::text AS proname, s.calls,
       ROUND(s.total_time::numeric, 2) AS total_ms,
       ROUND(s.self_time::numeric, 2) AS self_ms
FROM pg_stat_user_functions s
JOIN pg_proc p ON p.oid = s.funcid
JOIN pg_namespace n ON n.oid = p.pronamespace
ORDER BY {{order}} NULLS LAST
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'topproc.top_procs';

-- [vacuum.bloat_estimate]
--   **名字类(name)列改为 ::text 输出**:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS schema,
       c.relname::text AS "table",
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
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'vacuum.bloat_estimate';

-- [vacuum.dead_tuples]
--   **布尔列改为输出 1/0**(CASE WHEN … THEN 1 ELSE 0 END),名字类(name)列改为 ::text:业务库是 M 兼容模式时,现场中间件按 JDBC 类型映射取值再转成文字,类型不在 M 兼容 JDBC 映射表里的列可能取不到原值(2026-11-25 现场:布尔列取回来是 Java 字节数组 [B@…,健康检查报「无法识别的布尔取值」)。列名、筛选、排序与取值含义不变(布尔列由 true/false 变为 1/0,技能两种都认),A 兼容库上结果一致。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT n.nspname::text AS schema,
       t.relname::text AS "table",
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
       CASE WHEN ''autovacuum_enabled=false'' = ANY(c.reloptions) THEN 0 ELSE 1 END AS autovac_enabled,
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
-- 应为 133：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 新增的 1 条应全部在列：
--   SELECT script_name FROM grmp.script_config WHERE script_name IN ('sqltune.plan_yaml');
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('explain.relation_schemas', '408ddeb7b91395ff0fa53ad794b47ba1'),
--     ('health.partitions', 'f6e1b8ce279a7fe5d464dd24290457da'),
--     ('health.table_writes', '15f23c63dcdb1cb0e4d080bb35999ab7'),
--     ('health.unused_index', '3462a0a36063251844d0239adc5be44e'),
--     ('procinfo.locate_schema', 'bafffcec3535f22f739c887e69f79d11'),
--     ('procinfo.proc_def', '29a5bc2f5578ca1c12058e204b685c43'),
--     ('proctune.column_stats', '172aa48c7e995b4af9c7836c55dfabd3'),
--     ('proctune.indexes', '456b672c7a0fa35bcc52939416eece4c'),
--     ('proctune.locate_schema', 'bafffcec3535f22f739c887e69f79d11'),
--     ('proctune.proc_def', '29a5bc2f5578ca1c12058e204b685c43'),
--     ('proctune.tables', '8ae7339cf964d40aa572e26b721118c8'),
--     ('sqlreview.indexes', '19a4ba21ab26f485afc8df6a0f4392cf'),
--     ('sqlreview.tables', '521948c1c2b6b573968a4a165103e28a'),
--     ('sqltune.column_stats', '172aa48c7e995b4af9c7836c55dfabd3'),
--     ('sqltune.column_types', '043f909ba46a8d79fa8e30aabe4c8b87'),
--     ('sqltune.indexes', '71b10a0062fe0099d4de1be19cebc97d'),
--     ('sqltune.stats_freshness', 'b1b4b60b863280fe4dd11d02a6f81b9d'),
--     ('sqltune.tables', '8ae7339cf964d40aa572e26b721118c8'),
--     ('topproc.top_procs', 'c9b7b75f9dff3848494428089d5cadc7'),
--     ('vacuum.bloat_estimate', '7c57869002f7b31779614b55bfdf3659'),
--     ('vacuum.dead_tuples', 'eef6217b4dfc89438182caf0bf2d2d4d')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
-- 执行库应与本版一致（appbusiness 应为 40 条，且都以 dbName 开头）：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness';
--   SELECT script_name FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness'
--      AND parameter_config NOT LIKE '[{"key":"dbName",%';     -- 应返回 0 行
