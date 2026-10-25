---
id: S3-20250505-CBST-偶现update慢-网络抖动
title: 偶现update慢-网络抖动
system: CBST
instance: 未知
occurred_at: '2025-05-05'
engine: gaussdb
severity: S3
primary_factor: 网络抖动
secondary_factors: []
conclusion: 已确认
source: sources/CBST-问题分析报告-2025-05-05.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- cbst.cosp_asyn_task_dtl
signals:
- 单条 update 偶发秒级
- 网络重传
rules: []
---
## 现场
单条 update 偶发 2s。
## 判断
数据库侧执行 5ms,应用侧 2s,网络重传。
## 处置
网络组处理端口。
## 复发标志
数据库侧耗时正常。
