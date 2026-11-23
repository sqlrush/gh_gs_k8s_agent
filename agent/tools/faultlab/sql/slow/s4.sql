-- S4 列上套类型转换,索引失效(6 张表)。目标:3 分钟左右跑完。
--
-- 成因:`customers.code` 上有索引(idx_customers_code),但条件写成 `c.code::bigint = o.cust_id`
-- —— 转换套在**索引列**上,索引就用不上了,每取一行订单都要全表扫 20 万行客户。
--
-- **为什么用 max(c.name) 而不是 LIMIT 1:** 一开始写的是 `… LIMIT 1`,实测每行只要 0.29 毫秒
-- —— 因为 customers 是按 id 顺序存的、code 又正好随 id 递增,要找的那一行往往就在扫描的
-- 最前面,拿到就停了。那不是真的「全表扫描」,校准出来的规模会虚高十几倍。换成聚合函数
-- 之后每一行都必须扫完整张表,代价才稳定、才是现场的样子。
--
-- 现实里的样子:编码列建表时是 varchar,业务里拿整型去比。两边类型不一致,
-- 顺手加个 `::bigint` 就「跑通了」—— 跑通和跑得快是两件事。
SELECT o.id, o.amount,
       (SELECT max(c.name) FROM fl.customers c WHERE c.code::bigint = o.cust_id) AS cust,
       r.name AS region, ch.name AS channel, w.name AS warehouse,
       (SELECT count(*) FROM fl.order_items i WHERE i.order_id = o.id) AS items
  FROM (SELECT * FROM fl.orders WHERE id <= {cap}) o
  JOIN fl.regions    r ON r.id = o.region_id
  JOIN fl.channels  ch ON ch.id = o.channel_id
  JOIN fl.warehouses w ON w.id = (o.id % 300) + 1
 ORDER BY o.amount DESC
