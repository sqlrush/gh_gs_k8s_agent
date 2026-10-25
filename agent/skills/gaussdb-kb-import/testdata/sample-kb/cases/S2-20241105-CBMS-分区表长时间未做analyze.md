---
id: S2-20241105-CBMS-分区表长时间未做analyze
title: 分区表长时间未做analyze
system: CBMS
instance: 未知
occurred_at: '2024-11-05'
engine: gaussdb
severity: S2
primary_factor: alter table exchange 不更新统计信息,分区表长期无 analyze
secondary_factors: []
conclusion: 已确认
source: sources/CBMS-问题分析报告-2024-11-05.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- msc.cbms_custpckg_cust_idx
- STALE_STATS
signals:
- STALE_STATS
- 分区表 last_analyze 很久
- exchange partition
- 执行计划跳变
rules:
- GS-STAT-002
---
## 现场
业务反馈存过执行时间长,怀疑是一张表长时间未做 analyze 导致执行计划跳变。
## 判断
业务通过 gs_loader 将数据批量导入一张临时表,后续通过 alter table exchange 操作同步到分区表。由于当前 exchange 操作不更新统计信息,分区表长期无有效统计。
## 处置
临时规避方案:业务语句中执行完分区表 exchange 操作后,代码中增加 analyze 分区表逻辑(即 analyze msc.cbms_custpckg_cust_idx);本行规范 GS-STAT-002 要求 exchange 后显式 ANALYZE,**不依赖** autoanalyze。
## 复发标志
STALE_STATS 命中分区表且该表有 exchange 作业。
## 区分证据
与「临时表统计信息缺失」区分:变慢前刚做过 exchange partition,pg_stat_user_tables 里该分区 last_analyze 很旧;不是过程内新建的临时表。
