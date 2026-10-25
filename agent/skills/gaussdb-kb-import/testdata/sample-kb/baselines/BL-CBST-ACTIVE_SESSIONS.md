---
id: BL-CBST-ACTIVE_SESSIONS
type: baseline
title: CBST 活跃会话数
topics:
- conn
system: CBST
metric: active_sessions
unit: 个
metric_source: gaussdb-health 连接维度(ACTIVE_HIGH)
window: 5 分钟
value_kind: 瞬时值
---
## 摘要
CBST 活跃会话数平时 30–60,21:00 起批量窗口 150–200 属正常,超过 250 才算异常。
## 正常表现
日间 30–60;批量窗口 150–200;月末批量最高 230。
