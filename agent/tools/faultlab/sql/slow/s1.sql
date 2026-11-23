-- S1 相关子查询逐行执行(7 张表)。**不会在窗口内跑完,设计如此。**
--
-- 成因:payments.order_id 没有索引(schema.sql 里故意不建)。SELECT 列表里两个相关
-- 标量子查询,每取一行订单就把百万行的 payments 整表扫两遍;WHERE 里的 EXISTS 是
-- 非等值条件,优化器上拉不了,只能继续逐行跑。
--
-- 现实里的样子:报表 SQL 在开发库(几千行数据)上秒出,上线后订单表涨到百万级,
-- 同一条 SQL 永远跑不完 —— 因为代价是「外层行数 × 内层全表扫描」。
SELECT o.id, c.name AS cust, r.name AS region, ch.name AS channel, pr.name AS promo,
       (SELECT count(*) FROM fl.payments p WHERE p.order_id = o.id) AS pay_cnt,
       (SELECT max(p2.amount) FROM fl.payments p2
         WHERE p2.order_id = o.id AND p2.status = 'OK') AS max_pay
  FROM (SELECT * FROM fl.orders WHERE id <= {cap}) o
  JOIN fl.customers c ON c.id = o.cust_id
  JOIN fl.regions   r ON r.id = o.region_id
  JOIN fl.channels ch ON ch.id = o.channel_id
  JOIN fl.promos   pr ON pr.id = o.promo_id
 WHERE EXISTS (SELECT 1 FROM fl.shipments s
                WHERE s.order_id = o.id AND s.ship_date > o.order_date + 3)
 ORDER BY max_pay DESC NULLS LAST
