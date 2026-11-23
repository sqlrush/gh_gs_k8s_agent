-- S7 高频点查。单次零点几秒(实测 og5 上约 0.2 秒),不停地跑。
--
-- 成因:按 `cust_code` 查订单,而这一列**没有索引**(schema.sql 里故意不建)→ 每次都
-- 全表扫 100 万行。单看一次不算慢(数据在缓存里,零点几秒),但一天跑几万次,
-- 它就是整个库最耗资源的那条 SQL —— 这正是「单条不慢、累计最重」那一类问题。
--
-- 它和 S1 / S2 的分工:S1/S2 回答「哪条 SQL 单次最慢」,S7 回答「哪条 SQL 累计最耗」
-- —— Top SQL 的「总耗时」榜首与「平均耗时」榜首本来就不该是同一条,这一项用来钉住这件事。
SELECT count(*) AS n, sum(o.amount) AS amt
  FROM fl.orders o
  JOIN fl.customers c ON c.id = o.cust_id
 WHERE o.cust_code = {code}
