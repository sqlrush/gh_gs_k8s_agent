# 动态内存冲高归因方法论

供 LLM 按需查阅。脚本已产出确定性发现，本文提供根因判定树与整改方向。

## 根因判定树

从 L1 开始，按顺序收敛：

```
动态内存高？
├─ dynamic_used 低，但 dynamic_peak 高（MEM_PEAK_FALLBACK）
│    → 冲高已结束。走 history 模式回溯当时的 SQL 与算子；
│      现场视图（L1/L2/L3）已无当时数据。
│
├─ dynamic_used 持续高
│  ├─ L2：缓存类 context 占大头（CacheMemoryContext / SessionCacheMemoryContext）
│  │    → 元数据缓存膨胀 / 会话不释放。
│  │      跑 watch 确认是否单调上升（MEM_TREND_LEAK）后才可称「泄漏」。
│  │      常见诱因：表/分区数量极多、长连接不断累积 relcache、
│  │              连接池不回收连接。
│  │
│  ├─ L2：执行器类 context 占大头（ExecutorState / VecExecutor 等）
│  │    → 真在干活。继续下钻 L3 → L4 → L5。
│  │
│  └─ L2 不可用
│       → 直接下钻 L3 → L4 → L5，但在结论里说明「无法区分泄漏与真用量」。
│
└─ shared_used / other_used 高，dynamic 不高
     → 不是动态内存问题。shared 由 shared_buffers 等决定；
       other 是非托管内存（第三方库、元数据），排查方向不同。
```

## L3 → L4 → L5 收敛链

这三层是同一条线索，报告里用 `query_id` 串起来。归因时必须显式讲出整条链，
而不是三段孤立的表格：

| 层 | 拿到什么 | 交给下一层什么 |
|---|---|---|
| L3 | 哪个会话、哪个用户/应用、什么状态、在跑什么 SQL | 会话 → SQL 文本 |
| L4 | `query_id`、峰值内存、估算内存、下盘量、GaussDB 自带 warning | `query_id` |
| L5 | 该 `query_id` 下每个 `plan_node_id` 的算子名、峰值内存、下盘量、倾斜 | 具体算子 |

结论应形如：**动态内存 95% → etl 会话峰值 4.1 GB → query_id 90210 →
算子 #3 Vector Sort 峰值 3.8 GB、下盘 2.5 GB**。

## 典型信号与整改方向

| 发现代码 | 含义 | 整改方向 |
|---|---|---|
| `MEM_DYNAMIC_HIGH` | 动态内存逼近上限 | 定性信号，不是根因；继续下钻 |
| `MEM_PEAK_FALLBACK` | 曾冲高、现已回落 | 走 `history` 模式回溯 |
| `MEM_CONTEXT_DOMINANT` | 单个 context 占大头 | 缓存类 → 查表/分区数量与连接池；执行器类 → 下钻 SQL |
| `MEM_CONTEXT_FRAGMENT` | 空闲内存未归还 | 内存碎片；通常无需处理，除非持续增长 |
| `MEM_SESSION_HOG` | 单会话吃大内存 | 定位其 SQL；评估业务影响后再谈是否终止 [需人工执行] |
| `MEM_SESSION_IDLE_XACT` | 空闲事务占内存 | 查应用连接池是否未提交事务；`idle_in_transaction_session_timeout` |
| `MEM_SQL_ESTIMATE_OFF` | 估算内存与实际差 ≥10× | 统计信息过期 → `ANALYZE <table>` [需人工执行] |
| `MEM_OP_ROWS_OFF` | 估算行数与实际差 ≥10× | 同上；这是内存估算错的根因 |
| `MEM_SQL_SPILL` / `MEM_OP_SPILL` | 下盘 | work_mem 不足。**先定位到算子**再评估调 work_mem |
| `MEM_OP_SKEW` | 各 DN 内存倾斜 | 数据分布不均，检查分布列选择 |
| `MEM_CONFIG_OVERCOMMIT` | work_mem × 并发 > 动态内存上限 | 配置性风险，非当前故障直接原因 |
| `MEM_TREND_LEAK` | 采样期内单调上升未回落 | 疑似泄漏；结合 L2 的 context 归因 |
| `MEM_TREND_SPIKE` | 尖峰后回落 | 单次大查询，不是泄漏 |

## work_mem 调优的克制原则

`work_mem` 是**每算子、每连接**生效的，不是全局池。调大它会同时放大所有并发查询的
内存上限，理论最坏情况是 `work_mem × max_connections`（脚本的 `MEM_CONFIG_OVERCOMMIT`
就是在算这个）。

因此顺序应当是：

1. 先定位到**具体算子**（L5），确认是排序/哈希下盘导致；
2. 优先修**根因**——统计信息过期导致的行数低估（`MEM_OP_ROWS_OFF`）修好后，
   优化器往往会选更省内存的计划，根本不需要调 work_mem；
3. 确需调整时，**在会话级临时调**（`SET work_mem`）验证效果，不要直接改全局；
4. 全局调整前，用 `MEM_CONFIG_OVERCOMMIT` 重新核算理论上限。

## 何时不能下「泄漏」结论

单次 `snapshot` 只是一个时间点的切片。context 占比高**不等于**泄漏——一个正在跑大排序的
库，执行器 context 本来就该占大头。要称「泄漏」，必须满足：

- `watch` 模式采样显示内存**单调上升且未回落**（`MEM_TREND_LEAK`），且
- L2 显示是**缓存类 context**（而非执行器 context）在增长。

两条不同时满足时，如实说「证据不足以判定泄漏，建议跑 watch 确认」。
