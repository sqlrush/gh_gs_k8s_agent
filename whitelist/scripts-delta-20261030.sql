-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-10-30）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 126 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 128 条）。
--
--   新增   2 条 INSERT
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
-- 一、新增 2 条
-- =====================================================================

-- [procinfo.source_adm] 存储过程源码(ADM_SOURCE 兜底)
--   读取：pg_catalog.adm_source
--   用途：按名字逐行取过程 / 函数 / 包体的定义。只在按 pg_proc 取不到时调用(开了对象隔离的库,普通账号查 pg_proc 受行级访问控制);需执行账号有 ADM_SOURCE 的读权限,没有权限时技能照常给出排查报告。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'procinfo.source_adm', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT owner, name, type, line, text
FROM pg_catalog.adm_source
WHERE lower(name) = lower(''{{name}}'')
  AND lower(type) IN (''procedure'', ''function'', ''package body'')
ORDER BY owner, type, line
LIMIT 20001;
', '[{"key":"name","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- [proctune.source_adm] 存储过程源码(ADM_SOURCE 兜底)
--   读取：pg_catalog.adm_source
--   用途：与 procinfo.source_adm 相同,供存储过程调优在 pg_proc 取不到源码时使用。只读
INSERT INTO grmp.script_config (id, script_type, script_name, database_type, refered_appbusiness, kernel_version, region, deployment_form, execute_node_type, cluster_deployment_mode, script_content, parameter_config, scene, is_valid, create_user, create_time, last_modify_user, last_modify_time, is_asyn, "extend", compliance_mode, uuid) VALUES (grmp.script_config_seq.nextval, 'SQL', 'proctune.source_adm', 'appbusiness', 1, 'ALL', NULL, NULL, NULL, 'centralization', 'SELECT owner, name, type, line, text
FROM pg_catalog.adm_source
WHERE lower(name) = lower(''{{name}}'')
  AND lower(type) IN (''procedure'', ''function'', ''package body'')
ORDER BY owner, type, line
LIMIT 20001;
', '[{"key":"name","value":"","type":"STRING","autoAcquire":false}]', 'AGENT', 1, '999999999', now(), '999999999', NULL, 0, NULL, 'ALL', uuid());

-- =====================================================================
-- 二、更新 0 条已有脚本的 SQL 正文
-- =====================================================================

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 128：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 新增的 2 条应全部在列：
--   SELECT script_name FROM grmp.script_config WHERE script_name IN ('procinfo.source_adm','proctune.source_adm');
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('-', '-')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
