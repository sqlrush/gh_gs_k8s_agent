---
id: EP-CBST
type: profile
title: CBST 核心账务系统画像
system: CBST
aliases:
- cbst
product: GaussDB 集中式
version: 505.2.0
deployment: 一主两备
as_of: 2026-09
---
## 摘要
CBST 核心账务:GaussDB 集中式 505.2,一主两备;21:00 起批量、工作日上午联机高峰。
## 部署拓扑
一主两备,同城双机房。
## 连接池
应用 druid,单实例上限 200,开 removeAbandoned。
## 批量窗口
21:00–02:00 日终批量;月末加批。
## 关键业务依赖
联机记账、日终清算、对账。
## 高峰时段
工作日 9:30–11:30。
