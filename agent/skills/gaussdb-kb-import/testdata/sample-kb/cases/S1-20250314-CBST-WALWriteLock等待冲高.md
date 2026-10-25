---
id: S1-20250314-CBST-WALWriteLock等待冲高
title: WALWriteLock等待冲高
system: CBST
instance: 未知
occurred_at: '2025-03-14'
engine: gaussdb
severity: S1
primary_factor: 批量单事务提交 20 万行且 synchronous_commit=on,WAL 刷盘串行化
secondary_factors: []
conclusion: 已确认
source: sources/CBST-问题分析报告-2025-03-14.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- cbst.core_acct
- wal_buffers
- synchronous_commit
- LWLock:WALWriteLock
signals:
- WAIT_LWLOCK_HEAVY
- LWLOCK_EVENT
- WALWriteLock
- LWLock 等待占 DB time 高
rules:
- GS-WAIT-001
- GS-CHG-003
---
## 现场
批量期间 WAIT_LWLOCK_HEAVY 告警,等待事件 top1 是 WALWriteLock,占 DB time 40%,联机 TPS 跌六成。
## 判断
批量单事务 20 万行,提交时 WAL 刷盘串行;synchronous_commit=on 让每次提交都等 fsync。
## 处置
拆批到 5000 行/事务;批量窗口内经变更审批把 synchronous_commit 临时设为 local(窗口结束恢复);**不**按通用做法调 wal_buffers——实测无效。走变更单、23:00–06:00 窗口、双人复核(GS-CHG-003)。
## 复发标志
WAIT_LWLOCK_HEAVY 且 top 等待事件为 WALWriteLock,时间与批量窗口重合。
