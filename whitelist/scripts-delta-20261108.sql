-- =====================================================================
-- GRMP 诊断脚本白名单 · 增量导入（2026-11-08）
-- =====================================================================
--
-- 适用前提：已导入过上一版的 137 条脚本。本文件只含相对那一版的**增量**，
-- 不要整份重导（整份见同目录 scripts.sql，共 137 条）。
--
--   新增   0 条 INSERT
--   更新   4 条 UPDATE
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
-- 一、新增 0 条
-- =====================================================================

-- =====================================================================
-- 二、更新 4 条已有脚本的 SQL 正文
-- =====================================================================

-- [health.slow_sql]
--   **多取一列 user_name**(执行账号):云平台运维账号(rdsAdmin 等)跑的监控采样 SQL 按账号排除出业务慢 SQL (行方 11-08 测试报告:榜首是 rdsAdmin 的 ASP 采样 SQL)。筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  unique_sql_id::text,
  LEFT(REGEXP_REPLACE(query, ''\s+'', '' '', ''g''), 180) AS query,
  n_calls AS calls,
  ROUND((total_elapse_time/NULLIF(n_calls,0))/1000::numeric, 2) AS avg_ms,
  ROUND(total_elapse_time/1000000::numeric, 2) AS total_sec,
  ROUND(cpu_time/1000000::numeric, 2) AS cpu_sec,
  n_returned_rows AS rows,
  user_name
FROM dbe_perf.statement
WHERE (total_elapse_time/NULLIF(n_calls,0))/1000 > {{threshold_ms}}
  AND n_calls > 0
ORDER BY total_elapse_time/NULLIF(n_calls,0) DESC
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.slow_sql';

-- [health.storage]
--   **只量执行账号有权限的库与表空间**:库大小加 has_database_privilege(CONNECT)、表空间大小加 has_tablespace_privilege(CREATE) 或「本库默认表空间」的判断,没权限的那行返回 NULL(报告写「无权限查看」)。原先对每个库、每个表空间都量,受限账号碰到 一个没权限的就整条报错(行方 11-08 测试报告:健康检查 Storage 维度 HTTP 400)。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT ''database'' AS kind,
       d.datname  AS name,
       CASE WHEN has_database_privilege(d.oid, ''CONNECT'')
            THEN pg_database_size(d.oid)::bigint END AS bytes
  FROM pg_database d
 WHERE d.datallowconn
UNION ALL
SELECT ''tablespace'' AS kind,
       t.spcname    AS name,
       CASE WHEN has_tablespace_privilege(t.oid, ''CREATE'')
              OR t.oid = (SELECT dattablespace FROM pg_database WHERE datname = current_database())
            THEN pg_tablespace_size(t.oid)::bigint END AS bytes
  FROM pg_tablespace t
 ORDER BY bytes DESC NULLS LAST;
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'health.storage';

-- [slowsql.slow_sql]
--   **多取一列 user_name**(执行账号),用途同 health.slow_sql。筛选、排序不变。只读。
UPDATE grmp.script_config
   SET script_content   = 'SELECT
  unique_sql_id::text,
  LEFT(REGEXP_REPLACE(query, ''\s+'', '' '', ''g''), 180) AS query,
  n_calls AS calls,
  ROUND((total_elapse_time/NULLIF(n_calls,0))/1000::numeric, 2) AS avg_ms,
  ROUND(total_elapse_time/1000000::numeric, 2) AS total_sec,
  ROUND(cpu_time/1000000::numeric, 2) AS cpu_sec,
  n_returned_rows AS rows,
  user_name
FROM dbe_perf.statement
WHERE (total_elapse_time/NULLIF(n_calls,0))/1000 > {{threshold_ms}}
  AND n_calls > 0 AND last_updated >= CAST(''{{begin_time}}'' AS TIMESTAMP)
ORDER BY total_elapse_time/NULLIF(n_calls,0) DESC
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'slowsql.slow_sql';

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
  n_tuples_written AS rows_written,
  user_name
FROM (SELECT unique_sql_id,
             max(query)             AS query,
             sum(n_calls)           AS n_calls,
             sum(total_elapse_time) AS total_elapse_time,
             sum(n_returned_rows)   AS n_returned_rows,
             sum(n_blocks_hit)      AS n_blocks_hit,
             sum(n_blocks_fetched)  AS n_blocks_fetched,
             sum(cpu_time)          AS cpu_time,
             sum(data_io_time)      AS data_io_time,
             sum(n_tuples_inserted + n_tuples_updated + n_tuples_deleted) AS n_tuples_written,
             CASE WHEN count(DISTINCT user_name) = 1 THEN max(user_name) END AS user_name
        FROM dbe_perf.statement
       GROUP BY unique_sql_id) s
WHERE n_calls > 0
ORDER BY {{order}}
LIMIT {{limit}};
',
       last_modify_user = '999999999',
       last_modify_time = now()
 WHERE script_name = 'topsql.top_sql';

-- =====================================================================
-- 三、导入后自检
-- =====================================================================
-- 应为 137：
--   SELECT count(*) FROM grmp.script_config WHERE scene = 'AGENT';
-- 本次没有新增脚本。
-- 更新应已生效（应返回 0 行；库编码为 UTF8 时 md5 与这里一致）：
--   SELECT c.script_name FROM grmp.script_config c
--     JOIN (VALUES
--     ('health.slow_sql', 'b7c7ee40a90f0d52bffc2be7bad3b185'),
--     ('health.storage', '15f066d47238c5a1453e7d04958715ce'),
--     ('slowsql.slow_sql', '405627bea9f1631464701ca44c51615f'),
--     ('topsql.top_sql', '1d6881b03063b748466dcba08a3b1837')) AS v(name, sum) ON c.script_name = v.name
--    WHERE md5(c.script_content) <> v.sum;
-- 参数定义也一起更新了的（应列出这些脚本、且 parameter_config 与 scripts.sql 里一致）：
--   SELECT script_name, parameter_config FROM grmp.script_config WHERE script_name IN ('-');
