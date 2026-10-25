---
id: RB-LCK-001
type: runbook
title: 阻塞链处置:定位根会话并交应用方处理
topics:
- lock
source: sources/CBST-运维手册-v3.docx#4.2
verification: 已验证
applies:
  product: GaussDB 集中式
  versions: '>= 503'
not_applies: 根会话是主备同步、备份或变更窗口内的 DDL
approval:
- GS-XACT-001
---
## 摘要
阻塞链只处理根会话:数据库侧定位根会话后交应用运维平台由应用方处理,不直接终止业务会话;值班经理审批后才可在数据库侧终止根会话。
## 适用场景
锁等待超过 30 秒、阻塞链 ≥3 层,根会话 idle in transaction。
## 执行前提
按 GS-XACT-001,数据库侧终止业务会话必须有值班经理审批。
## 影响
根会话所在交易回滚,应用按重试策略重提。
## 步骤
1. lockwait 定位根会话与 application_name / client_addr;2. 通知应用运维平台;3. 应用方重启连接池或提交;4. 超过 10 分钟未解除,值班经理审批后数据库侧终止根会话。
## 验证方法
再跑 lockwait,阻塞链消失、活跃会话回落。
## 回退步骤
无需回退;交易由应用重试。
