---
id: S3-20250717-CBST-大表autoanalyze未触发
title: 大表autoanalyze未触发
system: CBST
instance: 未知
occurred_at: '2025-07-17'
engine: gaussdb
severity: S3
primary_factor: 大表更新比例未达 autovacuum_analyze_scale_factor
secondary_factors: []
conclusion: 已确认
source: sources/CBST-问题分析报告-2025-07-17.docx#前言
entered_by: sample
entered_at: '2026-09-02'
objects:
- cbst.trans_journal
- autovacuum_analyze_scale_factor
signals:
- STALE_STATS
- last_autoanalyze 很久
- 大表
rules:
- GS-STAT-002
---
## 现场
trans_journal 每天新增 200 万行,但 3 个月没 autoanalyze。
## 判断
表 8 亿行,默认 scale_factor 0.1 要 8000 万行变更才触发。
## 处置
本行对 >1 亿行的表表级设 autovacuum_analyze_scale_factor=0.01(GS-STAT-002),其他表不动。
## 复发标志
STALE_STATS 命中超大表。
