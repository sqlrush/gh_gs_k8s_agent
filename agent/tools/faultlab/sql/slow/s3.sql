-- S3 NOT IN 碰上可空列(5 张表)。目标:8 分钟左右跑完。
--
-- 成因:`customers.code` 有约 1% 的 NULL。带 NULL 的 NOT IN **不能**被改写成反连接
-- (NULL 的三值逻辑让「不在其中」无法判定),优化器只能对外层每一行重跑一遍子查询;
-- 子查询里又写了 `c.grade + 0 = r.zone` —— 列上套了算术,索引用不上,每次都全表扫。
--
-- 现实里的样子:「排除黑名单客户」。写成 NOT IN 看着最自然,可空列让它变成逐行子计划;
-- 更隐蔽的是结果也不对 —— 只要子查询里有一个 NULL,整个 NOT IN 就一行都不返回。
SELECT count(*) AS kept, sum(o.amount) AS amt
  FROM (SELECT * FROM fl.orders WHERE id <= {cap}) o
  JOIN fl.regions   r ON r.id = o.region_id
  JOIN fl.channels ch ON ch.id = o.channel_id
  JOIN fl.promos   pr ON pr.id = o.promo_id
 WHERE o.cust_code NOT IN (SELECT c.code FROM fl.customers c WHERE c.grade + 0 = r.zone)
