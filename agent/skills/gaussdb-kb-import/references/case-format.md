# 案例文件格式(`<kb>/cases/S<级>-YYYYMMDD-<系统>-<标题>.md`)

一份工单 / 问题分析报告结构化之后的样子。参考现场 gaussdb-rootcause 的 S1 文件,现场 / 判断 / 处置三节必填 + 头部字段;
2026-10-24 起案例记**排查思路**,可选小节:候选原因 / 取证顺序 / 排除理由 / 最终验证 / 区分证据(见下)。
**文件名就是案例 ID**,由 `kb.py review` 按 `severity / occurred_at / system / title` 生成,永不复用。

```markdown
---
id: S2-20250224-CBST-偶现单条update慢
title: 偶现单条 update 走索引耗时 3s
system: CBST                      # 业务系统
instance: 未知
occurred_at: 2025-02-24
engine: gaussdb                   # gaussdb | opengauss
severity: S2                      # S1–S4
primary_factor: autovacuum 尾部空页回收持 8 级锁与 DML 互相 cancel     # 根因一句话
secondary_factors: []
conclusion: 已确认                 # 已确认 | 推测 | 待验证 → 本案例所有边的置信度 1.0 / 0.6 / 0.3
source: sources/tickets.xlsx#row=18          # 出处:原文快照 + 定位,必填
entered_by: 12345
entered_at: 2026-09-02
objects: [cbst.cosp_asyn_task_dtl, autovacuum_vacuum_threshold]   # 表 / GUC / 等待事件 / 错误码,原样
signals: [单条 update 偶发秒级, autovacuum 频繁触发]                 # 复发标志:再发时能观察到什么
rules: [GS-VAC-002]               # 引用的客户条款 ID,可空
topics: [vacuum]                  # 所属专题(kb.py topic --list),决定进哪个 L1 专题导航
evidence: [evidence/20250224-慢日志.png]   # 可选:照片、日志等原始证据(kb.py attach 存档)
---
## 现场
业务偶现单条 update 走索引执行耗时 3s。(现场背景:CBST 日间联机、505.2 主备,非批量窗口)
## 候选原因
锁等待;计划跳变;autovacuum 干扰。
## 取证顺序
先看 pg_locks 有无等待 → 再看执行计划是否变化 → 最后看该表 autovacuum_count 与发生时刻是否重合。
## 排除理由
无锁等待记录;计划稳定走索引。
## 判断
autovacuum 检测到表尾部空页时触发 page 回收,持 8 级锁;正常 DML 进来会 cancel 掉 autovacuum…
## 处置
针对 cbst.cosp_asyn_task_dtl 小表,调大 autovacuum_vacuum_threshold 减少 vacuum 频率。
## 最终验证
调大阈值后一周内该表 autovacuum 次数下降 90%,未再出现 3s update。
## 复发标志
单条 update 偶发 3s 且该表 autovacuum 次数异常高。
## 区分证据
与「锁等待导致 update 慢」区分:本案发生时 pg_locks 无等待、该表 autovacuum 恰在同一秒触发。
```

小节顺序固定:现场 → 候选原因 → 取证顺序 → 排除理由 → 判断 → 处置 → 最终验证 → 复发标志 → 区分证据;
只认这九个名字,其余小节名是 error。**区分证据**在本行同一现象有过不同根因时必须写(看起来相同、根因不同的反例):
L1 专题导航把这些案例并排列出,诊断时模型先按区分证据取证;`validate` / `review` 会点名没写的。

## 各部分进哪个库

| 部分 | SQLite 索引 `index/store.sqlite`(向量 + 词法) | Neo4j(图,可选) |
|---|---|---|
| 头部字段 | 摘要块 + 元数据过滤(system / occurred_at / conclusion) | 案例节点 `case:<id>`;`objects` → `INVOLVES` 边;`rules` → `REFERENCES` 边 |
| 现场 | 一块(块首 `标题 › 现场`),带信号权重 | `(:Case)-[:EXHIBITS]->(:Symptom)` |
| 判断 | 一块 | `(:Symptom)-[:CAUSED_BY]->(:RootCause)` |
| 处置 | 一块 | `(:RootCause)-[:HANDLED_BY]->(:Action)` |
| 复发标志 | 一块,带信号权重;并入现象节点的 `signals` | — |

`conclusion` 决定案例自带边的置信度;模型抽取、用户在选择列表里**接受**的边 = 1.0,没答的留 candidate(< 1,不进路径)。

## 校验(`kb.py validate` / `review`)

- 必填:id / title / system / occurred_at / conclusion / source;`## 现场 ## 判断 ## 处置` 三节非空;
- `id` = 文件名,格式 `S[1-4]-YYYYMMDD-<系统>-<标题>`;`conclusion` ∈ 已确认/推测/待验证;`severity` ∈ S1–S4;
- 没有「复发标志」也没有 `signals` 只告警——但按发现匹配案例靠它,尽量补;
- 案例 ID 重复、`source` 无 `#` 定位:前者 error,后者 warn。
