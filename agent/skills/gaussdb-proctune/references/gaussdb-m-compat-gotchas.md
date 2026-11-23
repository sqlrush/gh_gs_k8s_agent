# GaussDB M 兼容（MySQL 兼容模式）写法与陷阱

> 何时相关：登录输出「兼容模式 M」（`sql_compatibility = 'M'`，建库时 `dbcompatibility = 'M'`）的业务库；报告开头有「业务库兼容模式：M」一行时也是。
> 价值：在 M 兼容库上，**你写的改写 SQL、建索引语句要按 M 的语法写**，按 A / PostgreSQL 的写法会直接报语法错，或者不报错但意思变了。
> 依据：GaussDB 25.1.32《M-Compatibility 开发者指南》（下文各节注明出处）。M 兼容库与 MySQL 5.7 大体兼容，但执行计划、EXPLAIN 输出沿用 GaussDB。
> 标 ⚠️ 的是「不报错、结果却不一样」的坑：改写前后结果是否等价，交给 `verify.py` 的等价门判，不要自己断言。

---

## 1. 标识符用反引号，双引号是字符串 ⚠️

`[M]` 标识符默认用反引号：`` SELECT `order`, `key` FROM `t_order` ``。**双引号默认是字符串常量**，只有 sql_mode 含 `ANSI_QUOTES` 时才是标识符。（出处：SQL 语法 › 标识符说明）

```sql
-- ❌ M 库里 "OrderNo" 是字符串 'OrderNo'，这一列每行都是同一个值，不报错
SELECT "OrderNo" FROM t_order;
-- ✅
SELECT `OrderNo` FROM t_order;
```

保留字作名字必须加反引号：`TABLE`、`INDEX`、`KEY`、`ARRAY`、`LIMIT`、`OFFSET` 等在 M 里是保留字，`AS table` 直接报语法错。（出处：SQL 语法 › 关键字）

## 2. `||` 是逻辑或，拼接用 `concat()` ⚠️

`[M]` `'a' || 'b'` 是逻辑或，不是拼接（2026-11 现场：`n.nspname || '.' || c.relname` 报 argument of OR/AND must be type boolean）。拼接写 `concat(a, '.', b)`。

`concat()` 的参数里有一个是 NULL，结果就是 NULL（MySQL 语义）；A 兼容库里 NULL 当空串。可能为空的列先判空：`CASE WHEN c IS NULL THEN '' ELSE concat(…) END`。

## 3. 不支持 `ILIKE`

`[M]` 没有 `ILIKE`（syntax error at or near "ILIKE"），也没有 `~~*`。不分大小写的匹配写 `lower(col) LIKE lower('%x%')`。
M 库里 `LIKE` 本身是否区分大小写取决于列的字符序（`*_ci` 不区分），改写时不要假设两者行为相同。⚠️

## 4. 类型转换：CAST 的目标类型有限，`::` 不建议

`[M]` `CAST(x AS 类型)` 的目标类型只有：`SIGNED`、`UNSIGNED`、`DATE`、`TIME`、`DATETIME`、`BINARY`、`CHAR`、`DECIMAL`、`FLOAT`、`DOUBLE`、`JSON`。
`CAST(x AS float8)`、`CAST(x AS bigint)`、`CAST(x AS text)` 这类 A 库写法在 M 库不行。双冒号 `x::int` 能用，但官方「不建议」（结果可能不符合预期），改写里不要用。（出处：SQL 参考 › 类型转换 › 显式类型转换）

```sql
-- ❌
WHERE CAST(order_no AS bigint) = 100
-- ✅
WHERE CAST(order_no AS SIGNED) = 100
```

索引列上套类型转换照样会让索引用不上——这条与 A 库相同。

## 5. 空串不是 NULL ⚠️

`[M]` `'' IS NULL` 为假，`length('')` 是 0；A 兼容库里 `''` 就是 NULL。从 A 库搬来的「`col = ''` 等于没值」的写法在 M 库意思不同。

## 6. 除法与整除

`[M]` `/` 的结果是小数；要整除写 `DIV`（`7 DIV 2` 得 3，M 专有，A 库没有这个写法）或 `floor(7 / 2)`。取余 `%` 与 `MOD` 都支持。（出处：函数 › 数字操作函数和算术操作符）

## 7. 排序与分页

`[M]` `ORDER BY … NULLS FIRST / NULLS LAST` 支持；升序默认 NULL 在后、降序默认 NULL 在前（与 A 库相同）。
`LIMIT n OFFSET m` 与 `LIMIT m, n` 都支持。（出处：SQL 参考 › SELECT）

## 8. 建索引

`[M]` 语法：`` CREATE [UNIQUE] INDEX [`索引名`] [USING BTREE] ON `表` (`列` [ASC|DESC], …) ``，索引名可省，但建议写上：
**索引名在 schema 内唯一**（MySQL 是表内唯一），起名带上表名，避免和别的表的索引重名。函数索引写 `((表达式))`。
（出处：SQL 参考 › CREATE INDEX；兼容性说明 › M-Compatibility 模式概述）

## 9. 函数用 M 支持的写法

`[M]` 与 MySQL 同名的原 GaussDB 函数（`round`、`position`、`instr`、`format`、`ceil`、`regexp_*` 等）行为可能与 MySQL 不同，改写里优先用 M 模式支持的函数；
`isEmpty`、`variance`、`stddev_pop`、`var_pop` 等在 M 库直接提示不支持。不要写 `pg_catalog.函数名(…)` 这种带 schema 的调用。（出处：系统函数 › 系统函数兼容性概述）

## 10. M 库没有的东西

`[M]` 物化视图、SPM 计划管理（SQL 计划基线）、全密态、DB4AI 不支持——不要把这些写进建议。（出处：M-Compatibility 数据库不支持的 GaussDB 特性）
系统视图只继承了一部分：`ADM_*` 视图（如 `ADM_SOURCE`）没有，`pg_stat_*`、`pg_stats`、`pg_locks`、`pg_settings` 等有。

## 11. 执行计划照旧

`[M]` EXPLAIN 用 GaussDB 的语法与输出（`EXPLAIN (FORMAT JSON) …`），不是 MySQL 的 `EXPLAIN FORMAT=…`；计划节点、代价的读法与 A 库相同，`tuning-methodology.md` 的检查清单照常用。
