# GaussDB A 兼容（Oracle 模式）陷阱

> 何时相关：`sql_compatibility = 'A'`（Oracle 兼容）的库，或从 Oracle 迁来的 SQL 与存储过程。
> 价值：这些是**改写等价性最易踩的语义差异**。A 兼容下很多「看着等价」的改写其实不等价。标 ⚠️ 的，未确认前不得断言等价——让 `verify.py` 的等价门判定。
> 判断模式：`SHOW sql_compatibility;`（'A'=Oracle / 'PG'/'B'=PostgreSQL-MySQL 系）；或建库时 `DBCOMPATIBILITY='A'`。

---

## 1. 空串 = NULL（头号坑）⚠️

`[GaussDB A]` Oracle 模式里**空字符串 `''` 被当作 NULL**：`'' IS NULL` 为真，`col = ''` 永远不命中。PG 模式里 `''` 是与 NULL 不同的合法空串。

影响：
- `WHERE col = ''` 与 `WHERE col IS NULL` 在 A 兼容下行为纠缠；迁移/改写时把两者互换会改变结果。
- 插入 `''` 实际存入 NULL（NOT NULL 列上可能直接报错）。
- 任何依赖「空串与 NULL 不同」的改写，在 A 兼容下都要重新确认。

## 2. NULL 与字符串连接 `||` ⚠️

`[GaussDB A]` Oracle 模式 `'a' || NULL = 'a'`（NULL 当空串）；PG 模式 `'a' || NULL = NULL`。

影响：拼接表达式（如拼 key、拼条件）在两种模式下结果不同；涉及 `||` 的改写必须按当前模式判等价。

## 3. ROWNUM 在 ORDER BY 之前生效 ⚠️

`[GaussDB A]` 支持 `ROWNUM` 伪列，但**先取行再排序**：

```sql
-- ❌ 取到任意 10 行再排序，不是 Top-10
SELECT * FROM t WHERE ROWNUM <= 10 ORDER BY x;
-- ✅ 先排序再截断
SELECT * FROM (SELECT * FROM t ORDER BY x) WHERE ROWNUM <= 10;
```

`ROWNUM <= n` ≈ `LIMIT n`，但改写成 `LIMIT` 时必须保证 ORDER BY 在截断之前，否则结果集变了。

## 4. `(+)` 外连接 ⚠️

`[GaussDB A]` 支持 Oracle 风格 `t1.a = t2.b(+)`（`(+)` 在哪侧，哪侧补 NULL，即对侧是 LEFT/RIGHT 外连接的保留表）。

改写成 ANSI `LEFT/RIGHT JOIN` 是常见清理，但易错：`(+)` 在 `t2` 列 = `t1` 是保留表 = `LEFT JOIN t2`。改写必须保留同一侧为外连接保留表；且 `(+)` 不能与 ANSI JOIN 混用、对常量谓词位置敏感。务必过 verify。

## 5. DUAL 表

`[GaussDB A]` 提供 `DUAL`：`SELECT sysdate FROM dual;`。改写/迁移到 PG 风格时去掉 `FROM dual`（PG 允许无 FROM 的 `SELECT`）。纯语法，等价。

## 6. 日期与时间 ⚠️

`[GaussDB A]`：`SYSDATE`、`TO_DATE(s, fmt)`、`TO_CHAR(d, fmt)` 用 Oracle 格式掩码；Oracle 的 `DATE` **含时分秒**（不是纯日期）。

影响：`TO_CHAR(d,'YYYY-MM-DD') = '...'` 改成范围时（见 rewrite-patterns §3），若 `d` 含时分秒，范围必须是半开区间 `>= 当天 00:00 AND < 次日 00:00`，不能用 `= 当天`。格式掩码大小写、`HH` vs `HH24` 等也影响等价。

## 7. NVL / NVL2 / DECODE → COALESCE / CASE

`[GaussDB A]` 提供 `NVL(a,b)`、`NVL2(a,b,c)`、`DECODE(e,k1,v1,...,def)`。

- `NVL(a,b)` ≈ `COALESCE(a,b)`，但 NVL 是两参、且隐式类型转换规则与 COALESCE 略不同（NVL 按第一参类型）。
- `DECODE` ≈ `CASE WHEN e=k1 THEN v1 ... ELSE def END`，但 **DECODE 把 NULL=NULL 视为相等**（CASE 的 `=` 对 NULL 不成立）——含 NULL 分支时不等价 ⚠️。

## 8. MINUS → EXCEPT

`[GaussDB A]` 支持 `MINUS`，等价于 `EXCEPT`（都去重）。注意若要保留重复语义需 `EXCEPT ALL`。

## 9. 隐式类型转换更宽松 ⚠️

A 兼容沿用 Oracle 较宽松的隐式转换（字符串↔数字↔日期）。这既掩盖了「隐式转换阻断索引」的问题（见 cbo §3.2），也使「字面量类型对齐」类改写在不同模式下表现不同。改写消除隐式转换时，确认转换方向与原结果一致。

## 10. 标识符大小写

Oracle 把未加引号标识符转**大写**，PG 转**小写**。A 兼容下未加引号标识符的大小写折叠行为与 PG 不同；跨模式比对 `pg_proc`/列名时注意。一般不影响性能，但影响「按名匹配」的脚本。

## 11. 包（package）与游标

`[GaussDB A]` 支持 package（含 package 级游标、状态变量、public/private）。proctune v1 把**包内游标、参数化游标、REF CURSOR 归 Skipped**（见 proc spec）。涉及这些只给建议，不抽取改写。

---

## 对改写 / 验证的小结

A 兼容库里，下列改写**默认带等价风险**，必须过 `verify.py` 等价门、不得直接断言：

- 任何涉及 `''` / NULL 的谓词或拼接（§1、§2）
- `ROWNUM` → `LIMIT`（§3）
- `(+)` → ANSI JOIN（§4）
- `TO_CHAR(date)=` → 范围（§6，注意 DATE 含时分秒）
- `DECODE` 含 NULL 分支 → CASE（§7）

判断一条 A 兼容 SQL/过程时，先 `SHOW sql_compatibility` 确认模式，再按上面逐条核对，避免把「PG 直觉」套到 Oracle 语义上。
