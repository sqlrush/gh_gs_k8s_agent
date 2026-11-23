-- S5 统计信息过期 → 估错选择率 → 选错计划(5 张表)。目标:8 分钟左右跑完。
--
-- 成因两条,叠在一起才成立:
--   ① `import_stage` 刚导入 50 万行、**没有做 ANALYZE**(V7,表级 autovacuum 也关了)。
--      没有统计信息时,优化器对 `cust_id = 1` 这种等值条件只能按默认选择率猜(约 0.5%),
--      估出来几千行;
--   ② 这批数据是**倾斜**的:三分之一的行都挂在 1 号客户名下,实际有 16 万多行。
--   估少了 60 多倍,于是「每行跑一次子查询」在优化器看来很便宜 —— 实际跑起来要跑十几万次,
--   而每次都在没有索引的 payments 上扫一遍百万行。
--
-- **这里有一条反直觉的事实,别再改回去:** 没有统计信息时,优化器对**表的总行数**估得很准
-- (它按物理页数反推,实测 499950 ≈ 50 万)。真正估不准的是**列的选择率** —— 没有直方图和
-- 高频值,等值条件只能按默认值猜。所以这个场景的钩子必须挂在 WHERE 的选择率上,
-- 挂在「表有多少行」上是造不出来的(第一版就是这么写的,结果优化器把子查询拉平成了
-- 哈希聚合,1.1 秒就跑完了)。
--
-- 看证据:`EXPLAIN ANALYZE` 里 import_stage 那一行的 rows= 估计值与实际值差几十倍。
-- 现实里的样子:凌晨批量导入后没收集统计信息,白天第一个查这张表的报表就卡死。
SELECT count(*) AS n, sum(x.avg_pay) AS s
  FROM (
    SELECT s.id,
           (SELECT avg(p.amount) FROM fl.payments p WHERE p.order_id = s.order_id) AS avg_pay
      FROM fl.import_stage s
      JOIN fl.orders    o ON o.id = s.order_id
      JOIN fl.customers c ON c.id = s.cust_id
      JOIN fl.regions   r ON r.id = c.region_id
      JOIN fl.channels ch ON ch.id = c.channel_id
     WHERE s.cust_id = 1
       AND s.id <= {cap}
  ) x
