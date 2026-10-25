# OpenGauss/GaussDB SQL 调优方法论

从 opendb tuner 提炼的清单。对着证据包逐条走;引用数字,不凭印象。

## 1. 计划走查

- 找最贵的节点(cost 占比最高 / 有 ANALYZE 时看 actual time)。
- 每个 Seq Scan:表大不大(`## Tables` 的 pages/tuples)?Filter 选择性高不高(`## Column Statistics` 的 n_distinct、null_frac)?
  - 选择性高的 filter + 大表 + 无匹配索引 → 索引候选。
  - n_distinct 负值是比例(如 -0.5 = 50% 不同值)。
- 每个 Sort:是否已有索引按 Sort Key 排序(`## Indexes` 的 DEF)?没有且该 sort 是热点,考虑建索引;查 `work_mem` 是否溢出(EXPLAIN ANALYZE 显示 "Sort Method: external")。
- Nested Loop:内层行数 × 外层循环数;内层若是 Seq Scan,连接键索引通常能赢。
- Hash Join 一般没问题;确认 build 侧能放进 work_mem。

## 2. 基数核对

- 比对计划估算行数 vs reltuples,以及(--analyze 时)实际行数。
- 偏差 >10× → 统计陈旧:先建议 `ANALYZE <table>;`(大表注明放低峰执行),并查倾斜列的 `default_statistics_target`。

## 3. 索引建议

- 复合索引列序:等值谓词在前,再范围,再 ORDER BY 列。
- 确认没有已存在的索引覆盖了该前缀(`## Indexes`)。
- 永远给出确切 DDL:`CREATE INDEX idx_<table>_<cols> ON <table>(<cols>);`
- 提醒:OpenGauss CREATE INDEX 持 ShareLock(阻塞写);建议错峰执行;CONCURRENTLY 语义在 OpenGauss 上有差异 —— 推荐前先在目标版本上验证。
- **只推荐串行建索引**:不要给 `WITH (parallel_workers=N)` 之类并行构建选项 —— 并行建索引对生产 CPU/IO 冲击大,策略上禁止(见 SKILL.md 生产温和性红线)。默认串行 + 低峰执行。

## 4. GUC 审查(对照 `## Key Parameters (GUC)`)

| 参数 | 经验法则 |
|---|---|
| work_mem | Sort/Hash 溢出 → 先会话级调高,别全局 |
| effective_cache_size | 应反映可用 OS 缓存;过低会让优化器偏离索引扫描 |
| random_page_cost | SSD 存储 → 1.1–2.0;默认 4 过度惩罚索引扫描 |
| default_statistics_target | 倾斜数据按列调高,然后 ANALYZE |

并行执行类参数(`max_parallel_workers_per_gather`、GaussDB 的 `query_dop`)**不作为调优建议**:
用并行换时延是把单条 SQL 的负载摊给整个系统,生产温和性策略禁止(见 SKILL.md)。

优先级:SQL 改写 > 索引 > 会话级 GUC > 全局 GUC(影响范围递增;全局 GUC 不直接给建议,只提示需运维评估)。

## 5. 改写模式

- `SELECT *` → 只投影需要的列(可启用 index-only scan)。
- 前导通配的 LIKE 用不了 btree;考虑 trigram / 全文检索。
- 跨列 OR → 各索引分支的 UNION ALL。
- 函数包住索引列会废掉索引;把函数挪到常量侧。
- 大 IN 列表 → 改成对 VALUES 的 JOIN。

## 6. 建议排序

按 (预期加速 × 置信度) / 风险 排序。给一条主建议;备选放在「若受约束不可用」下。
风险项里「对生产系统的资源冲击与锁影响」权重最高;同等收益取更温和的方案(见 SKILL.md 生产温和性红线)。
