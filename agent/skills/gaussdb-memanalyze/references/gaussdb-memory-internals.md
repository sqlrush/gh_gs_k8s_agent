# OpenGauss / GaussDB 内存架构（背景知识）

供 LLM 按需查阅。纯参考资料，不承载判定规则——判定在脚本里。

## 内存分区

`gs_total_memory_detail`（或等价视图）把实例内存拆成几块，单位 MB：

| memorytype | 含义 |
|---|---|
| `max_process_memory` | 进程可用内存总上限（GUC `max_process_memory`） |
| `process_used_memory` | 当前进程已用总量 |
| `max_dynamic_memory` | **动态内存上限**——查询执行、会话、内存上下文都从这里分配 |
| `dynamic_used_memory` | 当前动态内存已用量 |
| `dynamic_peak_memory` | 动态内存的**历史峰值**（进程启动以来） |
| `max_shared_memory` / `shared_used_memory` | 共享内存（shared_buffers 等），与动态内存是两个池子 |
| `other_used_memory` | 非托管内存：第三方库、元数据等，数据库统计不到细节 |

**关键区分**：动态内存冲高与共享内存冲高的排查方向完全不同。共享内存由
`shared_buffers` 等静态参数决定，不随查询波动；动态内存才是查询执行吃掉的那部分。

`dynamic_peak_memory` 是历史峰值且**不会回落**——它记录的是进程启动以来的最高水位。
所以「当前用量低、峰值很高」意味着冲高发生过并已结束，而不是当前有问题。

## 内存上下文（Memory Context）

GaussDB 继承了 PostgreSQL 的 memory context 树：内存按用途分组分配，整组释放。
常见 context：

| context | 用途 | 增长意味着 |
|---|---|---|
| `CacheMemoryContext` | relcache / catcache 等元数据缓存 | 表/分区极多，或长连接持续累积元数据 |
| `SessionCacheMemoryContext` | 会话级缓存 | 会话不释放（长连接、连接池不回收） |
| `ExecutorState` / 各类 Vec 执行器 context | 查询执行期分配 | 正在跑重查询——是**真用量**，不是泄漏 |
| `TempSmallContextGroup` | 临时小块分配 | 通常无意义，除非碎片率高 |

每个 context 有 `totalsize`（已申请）、`freesize`（已申请但空闲）、`usedsize`（真正在用）。
`freesize / totalsize` 高说明内存申请后未归还操作系统——**内存碎片**，通常无害，
除非持续增长。

**缓存类 context 增长 = 疑似泄漏；执行器类 context 增长 = 真在干活。**
这是区分两类根因最重要的一条线索。

## WLM 资源跟踪

SQL 级与算子级的内存数据来自 WLM（Workload Manager）的资源跟踪机制，由几个 GUC 控制：

| GUC | 默认 | 作用 |
|---|---|---|
| `enable_resource_track` | `on` | 总开关，关掉则 L4/L5 全无数据 |
| `resource_track_level` | `query` | `query` = 只记 SQL 级；**`operator` = 才记算子级** |
| `resource_track_cost` | 100000 | 只跟踪代价高于此值的作业——代价低的查询根本不会被记录 |
| `enable_resource_record` | `off` | **开了才把作业写进 `gs_wlm_*_info` 历史表** |

视图分三种时效：

| 视图后缀 | 内容 | 生命期 |
|---|---|---|
| `_statistics` | **正在运行**的作业 | 实时，作业结束即消失 |
| `_history` | 已完成的作业 | **只保留约 3 分钟** |
| `_info` | 已完成的作业 | 持久化，但**需要 `enable_resource_record = on`** |

这解释了为什么「昨天内存满了，今天来查」经常查不到东西：`_history` 早过期了，
而 `_info` 默认根本不写。

## 关键字段

`gs_wlm_session_*`（SQL 级）：

- `estimate_memory` —— 优化器**估算**要用的内存
- `max_peak_memory` —— **实际**峰值内存
- `max_spill_size` —— 下盘量（work_mem 装不下时溢出到磁盘）
- `warning` —— GaussDB 自己给出的告警文本
- `query_plan` —— 执行计划文本

`gs_wlm_operator_*`（算子级）：

- `plan_node_id` —— 算子在执行计划中的节点号，可与 `query_plan` 对上
- `plan_node_name` —— 算子名（Vector Sort / Vector Hash Join / Vector HashAggregate 等）
- `estimated_rows` vs `tuple_processed` —— 估算行数 vs 实际处理行数
- `max_peak_memory` / `max_spill_size` —— 该算子的峰值内存与下盘量
- `memory_skew_percent` —— 多 DN 之间的内存分配倾斜度

**估算 vs 实际的偏差是内存问题最常见的根因**：优化器低估行数 → 低估内存 → 分配不足 →
算子下盘或撑爆内存。修统计信息（`ANALYZE`）往往比调 `work_mem` 更治本。

## work_mem 的作用域

`work_mem` 是**每算子、每连接**的上限，不是全局池。一条 SQL 里有 3 个排序算子、
同时有 100 个连接在跑，理论最坏就是 `work_mem × 3 × 100`。这就是为什么盲目调大
`work_mem` 很危险——它把每一个并发查询的内存上限都放大了。

## 视图命名差异

openGauss 与 GaussDB、不同版本、集中式与分布式（`gs_` vs `pgxc_` 前缀）的内存视图
命名与列集都不一致。本 skill 因此在运行时探测视图与其真实列集，不硬编码——
探测结果印在报告的「能力与视图探测」节里。
