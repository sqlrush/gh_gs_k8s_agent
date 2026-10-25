---
id: S2-20250915-CBMS-exchange分区锁等待
title: exchange分区锁等待
system: CBMS
instance: 未知
occurred_at: '2025-09-15'
engine: gaussdb
severity: S2
primary_factor: exchange partition 与查询会话争锁
secondary_factors: []
conclusion: 已确认
source: sources/CBMS-问题分析报告-2025-09-15.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- cbms.trans_part
- LOCK_WAIT
signals:
- LOCK_WAIT
- exchange partition
- AccessExclusiveLock
rules:
- GS-CHG-003
---
## 现场
白天做 exchange 挂了 30 个查询。
## 判断
exchange 需要 AccessExclusiveLock。
## 处置
exchange 挪到 23:00–06:00 窗口(GS-CHG-003)。
## 复发标志
LOCK_WAIT 且持锁语句为 alter table exchange。
## 区分证据
与「批量与联机互锁」区分:持锁根会话的语句是 alter table … exchange partition,不是 update;发生在白天而非 21:00 批量窗口。
