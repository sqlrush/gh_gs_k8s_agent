-- faultlab 的全部库对象。只建在模式 fl 下,owner 是 fl_app。
--
-- **索引是故意不全的。** 下面几处「没建索引」不是遗漏,是场景的成因,手册里逐条写明:
--   · payments.order_id 没有索引  → S1 / S5 的相关子查询与嵌套循环每行扫一遍全表
--   · orders.cust_code 没有索引   → S3 的 NOT IN、S4 的列上套类型转换
--   · bloat_* / xmin_hold 只有主键 → VACUUM 场景只关心死元组,不需要别的索引
-- 改这个文件之前先看一眼 docs/test/og故障复现手册.md 的「为什么少了这些索引」。

CREATE SCHEMA IF NOT EXISTS fl;
SET search_path = fl, public;

-- ---------------------------------------------------------------- 维表(小)

DROP TABLE IF EXISTS regions CASCADE;
CREATE TABLE regions AS
  SELECT g AS id, 'region-' || g AS name, (g % 7) AS zone
  FROM generate_series(1, 500) g;
ALTER TABLE regions ADD PRIMARY KEY (id);

DROP TABLE IF EXISTS channels CASCADE;
CREATE TABLE channels AS
  SELECT g AS id, 'channel-' || g AS name, (g % 3) AS kind
  FROM generate_series(1, 200) g;
ALTER TABLE channels ADD PRIMARY KEY (id);

DROP TABLE IF EXISTS promos CASCADE;
CREATE TABLE promos AS
  SELECT g AS id, 'promo-' || g AS name, ((g % 30) + 1) AS discount
  FROM generate_series(1, 2000) g;
ALTER TABLE promos ADD PRIMARY KEY (id);

DROP TABLE IF EXISTS warehouses CASCADE;
CREATE TABLE warehouses AS
  SELECT g AS id, 'wh-' || g AS name, (g % 500) + 1 AS region_id
  FROM generate_series(1, 300) g;
ALTER TABLE warehouses ADD PRIMARY KEY (id);

DROP TABLE IF EXISTS couriers CASCADE;
CREATE TABLE couriers AS
  SELECT g AS id, 'courier-' || g AS name, (g % 300) + 1 AS warehouse_id
  FROM generate_series(1, 800) g;
ALTER TABLE couriers ADD PRIMARY KEY (id);

-- ---------------------------------------------------------------- 客户与商品

-- code 有约 1% 的 NULL —— S3 的 NOT IN 碰上可空列,靠的就是这些 NULL。
DROP TABLE IF EXISTS customers CASCADE;
CREATE TABLE customers AS
  SELECT g AS id,
         'cust-' || g AS name,
         CASE WHEN g % 97 = 0 THEN NULL ELSE lpad(g::text, 10, '0') END AS code,
         (g % 500) + 1 AS region_id,
         (g % 200) + 1 AS channel_id,
         (g % 5) + 1 AS grade
  FROM generate_series(1, 200000) g;
ALTER TABLE customers ADD PRIMARY KEY (id);
CREATE INDEX idx_customers_code ON customers (code);
CREATE INDEX idx_customers_region ON customers (region_id);

DROP TABLE IF EXISTS products CASCADE;
CREATE TABLE products AS
  SELECT g AS id,
         'product-' || g AS name,
         (g % 40) + 1 AS category_id,
         ((g % 900) + 10)::numeric(10, 2) AS price,
         (g % 300) + 1 AS warehouse_id,
         (g % 2000) + 1 AS promo_id
  FROM generate_series(1, 50000) g;
ALTER TABLE products ADD PRIMARY KEY (id);
CREATE INDEX idx_products_category ON products (category_id);

-- ---------------------------------------------------------------- 事实表(大)

-- 数据倾斜:每 3 笔订单里有 1 笔挂在 1 号客户身上(约 33 万笔)。
-- S5 要的「估 1 行、实际几十万行」就从这里来。
DROP TABLE IF EXISTS orders CASCADE;
CREATE TABLE orders AS
  SELECT g AS id,
         CASE WHEN g % 3 = 0 THEN 1 ELSE (g % 200000) + 1 END AS cust_id,
         ('2026-01-01'::date + (g % 280)) AS order_date,
         (ARRAY['NEW', 'PAID', 'SHIPPED', 'DONE', 'CANCELLED'])[(g % 5) + 1] AS status,
         ((g % 5000) + 1)::numeric(12, 2) AS amount,
         (g % 500) + 1 AS region_id,
         (g % 200) + 1 AS channel_id,
         (g % 2000) + 1 AS promo_id,
         lpad(((g % 200000) + 1)::text, 10, '0') AS cust_code
  FROM generate_series(1, 1000000) g;
ALTER TABLE orders ADD PRIMARY KEY (id);
CREATE INDEX idx_orders_cust ON orders (cust_id);
CREATE INDEX idx_orders_date ON orders (order_date);
CREATE INDEX idx_orders_region_date ON orders (region_id, order_date);
-- 故意不建:orders(cust_code)

DROP TABLE IF EXISTS order_items CASCADE;
CREATE TABLE order_items AS
  SELECT g AS id,
         (g % 1000000) + 1 AS order_id,
         (g % 50000) + 1 AS product_id,
         (g % 9) + 1 AS qty,
         ((g % 800) + 1)::numeric(10, 2) AS amount,
         'note-' || (g % 1000) AS note
  FROM generate_series(1, 3000000) g;
ALTER TABLE order_items ADD PRIMARY KEY (id);
CREATE INDEX idx_items_order ON order_items (order_id);
CREATE INDEX idx_items_product ON order_items (product_id);

-- payments.order_id **故意没有索引**:S1 的相关子查询、S5 的嵌套循环内层
-- 每取一行就扫一遍这张百万行表 —— 现场最常见的那类慢 SQL 就是这么来的。
DROP TABLE IF EXISTS payments CASCADE;
CREATE TABLE payments AS
  SELECT g AS id,
         (g % 1000000) + 1 AS order_id,
         ('2026-01-01'::date + (g % 280)) AS pay_date,
         ((g % 5000) + 1)::numeric(12, 2) AS amount,
         (ARRAY['CARD', 'WALLET', 'TRANSFER'])[(g % 3) + 1] AS method,
         (ARRAY['OK', 'FAILED', 'PENDING'])[(g % 3) + 1] AS status
  FROM generate_series(1, 1000000) g;
ALTER TABLE payments ADD PRIMARY KEY (id);

DROP TABLE IF EXISTS shipments CASCADE;
CREATE TABLE shipments AS
  SELECT g AS id,
         (g % 1000000) + 1 AS order_id,
         (g % 800) + 1 AS courier_id,
         (g % 300) + 1 AS warehouse_id,
         ('2026-01-01'::date + (g % 280) + (g % 7)) AS ship_date,
         (ARRAY['PACKED', 'SENT', 'ARRIVED'])[(g % 3) + 1] AS status,
         'TRK' || lpad(g::text, 12, '0') AS tracking
  FROM generate_series(1, 1000000) g;
ALTER TABLE shipments ADD PRIMARY KEY (id);
CREATE INDEX idx_shipments_order ON shipments (order_id);

-- ---------------------------------------------------------------- VACUUM 用

DROP TABLE IF EXISTS bloat_hot CASCADE;
CREATE TABLE bloat_hot AS
  SELECT g AS id, 0 AS cnt, repeat('x', 80) AS pad, now() AS touched_at
  FROM generate_series(1, 300000) g;
ALTER TABLE bloat_hot ADD PRIMARY KEY (id);

DROP TABLE IF EXISTS bloat_slow CASCADE;
CREATE TABLE bloat_slow AS
  SELECT g AS id, 0 AS cnt, repeat('y', 80) AS pad, now() AS touched_at
  FROM generate_series(1, 200000) g;
ALTER TABLE bloat_slow ADD PRIMARY KEY (id);

DROP TABLE IF EXISTS xmin_hold CASCADE;
CREATE TABLE xmin_hold AS
  SELECT g AS id, 0 AS cnt, repeat('z', 60) AS pad
  FROM generate_series(1, 100000) g;
ALTER TABLE xmin_hold ADD PRIMARY KEY (id);

-- V7:每次运行往这里导 50 万行、**不做 ANALYZE**,统计信息停在 0 行。
--
-- **必须挡住自动 ANALYZE,否则这个场景活不过一分钟:** autovacuum 里的自动
-- ANALYZE 会在变化量超过 10% 时自己来收集统计信息(autovacuum_analyze_scale_factor=0.1),
-- 导入完没多久统计信息就准了,S5 的「估错一个数量级」也就没了。
-- **光 autovacuum_enabled = false 不够:** og5 上实测,关了 autovacuum 的表照样被自动 ANALYZE
-- (2026-10-24 那轮导入 80 秒后就有了统计信息)。所以再把表级的 analyze 触发线拉到永远够不着。
-- 数值与 prepare.import_stage_options_sql() 一致,run 每次开跑前也会再设一遍。
DROP TABLE IF EXISTS import_stage CASCADE;
CREATE TABLE import_stage (
  id        bigint,
  order_id  bigint,
  cust_id   bigint,
  amount    numeric(12, 2),
  batch_at  timestamp
) WITH (autovacuum_enabled = false, autovacuum_analyze_threshold = 2000000000,
        autovacuum_analyze_scale_factor = 100);

-- ---------------------------------------------------------------- 锁与死锁用

DROP TABLE IF EXISTS acct_child CASCADE;
DROP TABLE IF EXISTS acct CASCADE;
CREATE TABLE acct (
  id   int PRIMARY KEY,
  bal  numeric(18, 2) NOT NULL DEFAULT 1000,
  note varchar(64)
);
INSERT INTO acct SELECT g, 1000, 'acct-' || g FROM generate_series(1, 2000) g;

-- 外键:K6 的父子锁冲突、D5 的外键死锁都靠它
CREATE TABLE acct_child (
  id       int PRIMARY KEY,
  acct_id  int NOT NULL REFERENCES acct (id),
  amount   numeric(18, 2) NOT NULL DEFAULT 1
);
INSERT INTO acct_child SELECT g, g, 1 FROM generate_series(1, 2000) g;

-- K7:多个会话插同一个主键,后来者等第一个事务的结果
DROP TABLE IF EXISTS uniq_race CASCADE;
CREATE TABLE uniq_race (
  k   int PRIMARY KEY,
  who varchar(32)
);

-- ---------------------------------------------------------------- 轻量锁用

-- L1:提交风暴的落点。每次 cleanup 清空,不然会一直长。
DROP TABLE IF EXISTS wal_log CASCADE;
CREATE TABLE wal_log (
  id        bigserial PRIMARY KEY,
  who       varchar(32),
  payload   varchar(200),
  wrote_at  timestamp DEFAULT now()
);

-- L2:100 行挤在同一个数据页上 —— 所有会话抢同一个缓冲区的内容锁。
-- 这里建的是普通表;prepare / run 随后会在 ustore 可用时把它换成 ustore 表(见 prepare.runtime_ddl)。
DROP TABLE IF EXISTS hotpage CASCADE;
CREATE TABLE hotpage AS
  SELECT g AS id, 0 AS cnt
  FROM generate_series(1, 100) g;
ALTER TABLE hotpage ADD PRIMARY KEY (id);
