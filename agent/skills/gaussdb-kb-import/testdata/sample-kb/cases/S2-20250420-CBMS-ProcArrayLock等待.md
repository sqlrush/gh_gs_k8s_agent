---
id: S2-20250420-CBMS-ProcArrayLock等待
title: ProcArrayLock等待
system: CBMS
instance: 未知
occurred_at: '2025-04-20'
engine: gaussdb
severity: S2
primary_factor: 短连接风暴
secondary_factors: []
conclusion: 已确认
source: sources/CBMS-问题分析报告-2025-04-20.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- LWLock:ProcArrayLock
- max_connections
signals:
- WAIT_LWLOCK_HEAVY
- ProcArrayLock
- 短连接
- 连接数瞬时冲高
rules:
- GS-CONN-001
---
## 现场
每分钟建连 3000 次,ProcArrayLock 等待占 DB time 25%。
## 判断
新上线模块每次请求新建连接;每次建连/断连都要拿 ProcArrayLock。
## 处置
应用接入本行统一连接池(GS-CONN-001 的 druid 参数模板),建连降到每分钟 20 次。
## 复发标志
WAIT_LWLOCK_HEAVY 且 top 是 ProcArrayLock;pg_stat_database 的 numbackends 抖动。
