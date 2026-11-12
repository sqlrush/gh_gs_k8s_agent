-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-12）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 138 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 132 条）。
--
--   新增   2 条 INSERT
--   更新   0 条 UPDATE
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
-- 一、新增 2 条
-- =====================================================================

-- [memanalyze.sql_mem_history] SQL 级内存排行（SQL 执行历史，WLM 资源跟踪不可用时的退路）
--   读取：dbe_perf.statement_history（unique_query_id、user_name、start_time、finish_time、used_memory、query 前 300 字）
--   用途：内存分析在 WLM 资源跟踪没开、视图没数据时，改从执行历史按单条语句内存用量取近一天的排行；在 postgres 库执行。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'memanalyze.sql_mem_history', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT unique_query_id AS queryid,
       user_name,
       start_time,
       ROUND(EXTRACT(EPOCH FROM (finish_time - start_time)) * 1000) AS duration_ms,
       used_memory,
       substr(query, 1, 300) AS query
  FROM dbe_perf.statement_history
 WHERE start_time > now() - interval ''1 day''
   AND used_memory > 0
 ORDER BY used_memory DESC
 LIMIT {{limit}};
', '[{"key":"limit","value":"","type":"INTEGER","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [wdr.redo_io_live] redo 文件写入的实时累计值（WDR 快照里没有这段时间的数据时的退路）
--   读取：dbe_perf.file_redo_iostat（phywrts、phyblkwrt 求和）
--   用途：WDR 写入来源在快照表没有数据时改读实时累计值，报告里写明「不是所选时间段」；实例级视图，在 postgres 库执行。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'wdr.redo_io_live', 'postgres', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT COALESCE(SUM(phywrts), 0) AS writes,
       COALESCE(SUM(phyblkwrt), 0) AS blocks
  FROM dbe_perf.file_redo_iostat;
', '[]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- =====================================================================
-- 二、更新 0 条已有脚本（SQL 正文 / 参数定义 / 执行库）
-- =====================================================================

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 132：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 新增的 2 条应全部在列：
--   SELECT script_name FROM grmp.script_config WHERE script_name IN ('memanalyze.sql_mem_history','wdr.redo_io_live');
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('-', '-')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
-- 执行库应与本版一致（appbusiness 应为 39 条，且都以 dbName 开头）：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness';
--   SELECT script_name FROM grmp.script_config WHERE scene = 'AGENT' AND database_type = 'appbusiness'
--      AND parameter_config NOT LIKE '[{"key":"dbName",%';     -- 应返回 0 行
