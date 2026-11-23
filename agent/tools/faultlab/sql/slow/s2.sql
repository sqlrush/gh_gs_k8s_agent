-- S2 多层派生表 + 关联键上套函数(7 张表)。**不会在窗口内跑完,设计如此。**
--
-- 两个毛病叠在一起:
--   ① 三层派生表,最内层对 300 万行做 GROUP BY,而会话的 work_mem 只有 4MB
--      → 哈希聚合落盘,产生临时文件;
--   ② 最外层用 `c.code LIKE l3.k || '%'` 关联 —— 前缀来自另一张表的列,索引用不上,
--      只能嵌套循环,20 万 × 20 万。
--
-- 现实里的样子:「按客户编码归集」的对账 SQL,编码前缀取多少位由参数决定,这里取满 10 位。
-- 开发时数据少,模糊匹配看不出代价;数据一多就再也跑不完。
-- (第一版取前 8 位只有约 2000 组,1~2 分钟就跑完了,不是「跑不完」。)
WITH lvl1 AS (
    SELECT i.order_id, sum(i.amount) AS amt, count(*) AS n
      FROM fl.order_items i
     GROUP BY i.order_id
), lvl2 AS (
    SELECT o.id, o.cust_code, o.order_date, l1.amt, l1.n
      FROM fl.orders o
      JOIN lvl1 l1 ON l1.order_id = o.id
     WHERE o.id <= {cap}
), lvl3 AS (
    SELECT substr(l2.cust_code, 1, 10) AS k, sum(l2.amt) AS amt, count(*) AS n
      FROM lvl2 l2
     GROUP BY 1
)
SELECT l3.k, l3.amt, l3.n, c.name AS cust, r.name AS region,
       w.name AS warehouse, cu.name AS courier, pdt.name AS product
  FROM lvl3 l3
  JOIN fl.customers  c ON c.code LIKE l3.k || '%'
  JOIN fl.regions    r ON r.id = c.region_id
  JOIN fl.warehouses w ON w.id = (c.id % 300) + 1
  JOIN fl.couriers  cu ON cu.id = (c.id % 800) + 1
  JOIN fl.products pdt ON pdt.id = (c.id % 50000) + 1
 ORDER BY l3.amt DESC
