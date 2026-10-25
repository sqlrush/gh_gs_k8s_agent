---
id: S2-20250611-CBST-异步任务日志表update抖动
title: 异步任务日志表update抖动
system: CBST
instance: 未知
occurred_at: '2025-06-11'
engine: gaussdb
severity: S2
primary_factor: autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel
secondary_factors: []
conclusion: 已确认
source: sources/CBST-问题分析报告-2025-06-11.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- cbst.cosp_asyn_task_log
- autovacuum_vacuum_threshold
signals:
- 单条 update 偶发秒级
- autovacuum 频繁触发
rules:
- GS-VAC-002
---
## 现场
异步任务日志表 update 平均 8ms,每小时有几十次 2–4s 的抖动。
## 判断
与 2 月 CBST 偶现 update 慢同一机制:小表频繁 autovacuum 尾部回收持 8 级锁。
## 处置
同 CBST 2 月案例:表级调大 autovacuum_vacuum_threshold。
## 复发标志
同表 autovacuum_count 增速高,update 偶发秒级。
