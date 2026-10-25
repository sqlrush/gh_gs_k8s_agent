---
id: S2-20250224-CBST-偶现单条update慢
title: 偶现单条update慢
system: CBST
instance: 未知
occurred_at: '2025-02-24'
engine: gaussdb
severity: S2
primary_factor: autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel
secondary_factors: []
conclusion: 已确认
source: sources/CBST-问题分析报告-2025-02-24.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- cbst.cosp_asyn_task_dtl
- autovacuum_vacuum_threshold
signals:
- 单条 update 偶发秒级
- autovacuum 频繁触发
- 小表尾部空页
- VACUUM_DEAD_RATIO
rules:
- GS-VAC-002
---
## 现场
业务偶现单条 update 走索引执行耗时 3s,平时 10ms 以内,无规律。
## 判断
autovacuum 检测到表尾部 1000 page 或尾部空页占比达 1/16 时触发 page 回收,持有 8 级锁;正常 DML 进来会 cancel 掉 autovacuum,cancel 与重试之间正好卡住这条 update。cbst.cosp_asyn_task_dtl 是小表但更新极频繁,阈值按默认值算每几分钟就触发一次。
## 处置
针对 cbst.cosp_asyn_task_dtl 这类小热表,表级调大 autovacuum_vacuum_threshold(本行口径:5 万),减少 vacuum 频率。**不要**按通用做法调小阈值让 vacuum 更勤——那正是本案的诱因。
## 复发标志
单条 update 偶发 3s 且该表 autovacuum_count 增速异常高;pg_stat_user_tables 里该表 n_dead_tup 很小。
