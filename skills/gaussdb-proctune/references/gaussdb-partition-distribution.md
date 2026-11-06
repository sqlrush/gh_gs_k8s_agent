# GaussDB 分区与分布（结构层知识）

> 何时相关：涉及分区表，或分布式部署（多 DN/CN）。
> 定位：这是**结构 / DDL 层**知识——本技能不自动改分区或分布列，相关结论进「建议（未验证）」。但要给得准，因为这一层没有 verify 闸。
> 与改写正例库的衔接：见 `gaussdb-rewrite-patterns.md` §12（分区裁剪触发）、§13（分布列对齐消除 REDISTRIBUTE）。

---

## 一、分区（单机即有，OLTP/OLAP 通用）

### 1.1 分区类型

| 类型 | 用途 |
|---|---|
| Range | 按区间（最常见：按日期/ID 范围） |
| List | 按枚举值（地区、状态） |
| Hash | 按 hash 均摊 |
| `[GaussDB]` Interval | 自动按区间扩展分区（到点自动建新分区，免手工 ADD） |
| 二级分区（Range-Range / Range-List 等） | 大表二维切分 |

### 1.2 分区裁剪（pruning）—— 性能核心

- **静态裁剪**：谓词在计划期即为常量 → 计划期就选定分区。
- **动态裁剪**：谓词含参数/join 键 → 运行期裁剪。
- **前提**：谓词作用在**分区键**且 **sargable**（无函数包裹、无隐式转换）。`WHERE to_char(part_key)=...` 会让裁剪失效，退化为全分区扫——和普通索引被函数阻断同源。
- **计划怎么看**：`[GaussDB]` 出现 `Partition Iterator` + `Selected Partitions: a..b`（或 iterations 数）。裁剪失败时选中分区 = 全部。

### 1.3 本地索引 vs 全局索引

- **LOCAL 索引**（默认）：每分区一份，随分区维护自动对齐，裁剪友好；但**唯一约束只能保证分区内唯一**。
- **GLOBAL 索引**：跨分区一份，可保证全表唯一（非分区键上的唯一约束需要它），但分区 DROP/TRUNCATE 会使其失效需重建。
- 建议判断：非分区键上的唯一/高选择性查询要全局唯一 → GLOBAL；否则优先 LOCAL（维护轻、裁剪好）。

### 1.4 分区维护（结构建议常用）

`ADD / DROP / TRUNCATE / SPLIT / MERGE / EXCHANGE PARTITION`。`EXCHANGE PARTITION` 可把普通表秒级换入做分区（快速数据加载/归档），`DROP PARTITION` 比 `DELETE` 快几个数量级（归档冷数据）。诊断「按时间删历史很慢」时，结构建议常是「改 Range 分区 + DROP PARTITION 归档」。

---

## 二、分布（仅分布式部署：多 DN/CN）

集中式（单机 openGauss）无分布列概念，但 SMP 并行仍有 `Streaming(LOCAL ...)`；下面针对**分布式部署**。

### 2.1 分布类型与分布列

| 分布方式 | 适用 |
|---|---|
| `DISTRIBUTE BY HASH(col)` | 大事实表，按高基数列散到各 DN |
| `[GaussDB]` REPLICATION | 小维表全量复制到每个 DN（消除 join 时的数据移动） |
| ROUNDROBIN | 无合适 hash 键时均摊 |
| RANGE / LIST（较新版本） | 按区间/枚举分布 |

**分布列**决定每行落在哪个 DN。选择原则：高基数、分布均匀（防倾斜）、**尽量等于高频 join 键**。

### 2.2 join 下推 vs REDISTRIBUTE / BROADCAST（最贵的成本）

- 两表 join 键 = 各自分布列 → join **下推为本地 join**，无跨 DN 数据移动（最优）。
- 不一致 → 计划出现：
  - `Streaming(type: REDISTRIBUTE)`：按 join 键把行跨 DN 重分布（**最贵：全量网络搬运**）。
  - `Streaming(type: BROADCAST)`：把一侧整表广播给所有 DN（小表广播可接受，**大表广播是灾难**）。
- 优化方向（结构建议）：让两张大表的**分布列对齐 join 键**消除 REDISTRIBUTE；小维表用 REPLICATION 免广播。

### 2.3 数据倾斜（skew）

分布列基数低或值倾斜 → 某些 DN 数据/负载远高于其他，木桶效应拖慢全局。
- 排查：`[GaussDB]` `table_skewness('schema.table')` 或 `gs_table_distribution` 看各 DN 行数分布。
- 建议：换更均匀的分布列，或对倾斜值特殊处理。

### 2.4 集中式 vs 分布式：Streaming 的不同含义

| 部署 | Streaming 来源 | 优化抓手 |
|---|---|---|
| 集中式 + SMP | 线程间并行（`LOCAL GATHER`/`LOCAL REDISTRIBUTE`） | `query_dop` 并行度；并行重分布均衡 |
| 分布式（多 DN） | 跨 DN 数据移动（`REDISTRIBUTE`/`BROADCAST`） | **分布列对齐 join 键**、小表 REPLICATION |

读到 `Streaming` 时先判断是哪种部署：集中式调并行度，分布式调分布设计。

---

## 给建议时的判断（小结）

涉及分区/分布的发现一律进「建议（未验证）」，并给到可执行方向：

- 计划全分区扫 / 无 `Partition Iterator` 裁剪 → 谓词没落在分区键或被函数包裹（见 rewrite §12，谓词改写可验证；分区设计本身不可自动改）。
- 大表 `REDISTRIBUTE`/`BROADCAST` → 分布列与 join 键不一致（建议对齐分布列或小表 REPLICATION，属建表 DDL，需人工评估与停机窗口）。
- 各 DN 行数悬殊 → 数据倾斜，建议换分布列。
- 「按时间删历史慢 / 冷数据多」→ 建议 Range/Interval 分区 + `DROP/EXCHANGE PARTITION`。

这些改动多为重建表/改分布的大动作，必须标注风险（停机、数据搬迁、全局索引重建），绝不当成轻量优化呈现。
