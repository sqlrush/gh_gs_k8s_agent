# 八类知识:类型、目录、必须记什么(导入时照此写,`validate` 照此校验)

**一条知识一个主类型;类型 = 目录**,frontmatter 的 `type` 必须与目录一致。混着几类的材料拆成多条、共用一份原文出处。
`[硬]` 缺了进不了库(error);`[要点]` 缺了提示补(warn)。

| 类型 | type / 目录 | ID | 必须记什么 |
|---|---|---|---|
| 公司内部规范 | 条款 `rules/<域>.yaml`;修正 `errata/` | `GS-<域>-NNN` | [硬] id / severity / check / rule;[要点] source、`approval`(审批与变更要求)、`effective`(生效版本 / 日期)、`applies`(适用范围)、`topics`、keywords |
| 历史案例排查思路 | `cases/` | `S<1-4>-YYYYMMDD-<系统>-<标题>` | [硬] 现场(含现场背景)/ 判断 / 处置、结论强度、出处;[要点] 候选原因、取证顺序、排除理由、最终验证、复发标志;同现象本行有别的根因时写**区分证据** |
| 环境与业务画像 | `profile` / `profiles/` | `EP-<系统>` | [硬] system;[要点] product、version、deployment、`aliases`(连接别名,诊断时靠它匹配本实例)、部署拓扑 / 连接池 / 批量窗口 / 关键业务依赖 / 高峰时段。**不写 IP、主机名、端口、口令**(校验拦截) |
| 指标口径与正常基线 | `baseline` / `baselines/` | `BL-<系统>-<指标>` | [硬] system、metric;[要点] unit、metric_source(哪个技能 / 视图)、window(采样窗口)、value_kind(累计值 / 差值 / 瞬时值)、`## 正常表现`(日间 / 批量 / 月末各多少) |
| 已知缺陷与版本例外 | `defect` / `defects/` | `KD-NNNN` | [硬] affected_versions;[要点] fixed_in(修复补丁 / 版本)、vendor_status(已确认 / 待确认)、现象、复现条件、规避办法 |
| 处置与恢复手册 | `runbook` / `runbooks/` | `RB-<域>-NNN` | [要点] verification、适用场景、**执行前提、影响**、步骤、**验证方法、回退步骤**;审批引用规范 ID |
| 高斯产品文档 | `product` / `product/` | `PD-NNNN` | [硬] **product**(GaussDB 集中式 / GaussDB 分布式 / openGauss / PostgreSQL / 通用)、**versions**;[要点] deployment。不写清系列与版本,openGauss / PostgreSQL 的行为就会被当成本环境的 |
| 存量 Wiki 经验 | `wiki` / `wiki/` | `WK-NNNN` | [要点] verification(已验证 / 未验证 / 已过时)、as_of(时间);可复用结论写进摘要,原文出处写 source |

六类类型化条目(画像 / 基线 / 缺陷 / 手册 / 产品文档 / Wiki)共同的:

- [硬] `id`(写 `auto` 由 `kb.py add` 分配)、`type`、`title`、`## 摘要`(≤200 字,结论在前;L1 只显示它);
- [硬] `source`(画像、基线除外)、`topics`(画像除外;专题清单 `kb.py topic --list`);
- 可选:`applies: {product, versions, systems, deployment}` 与 `not_applies`——**判断边界**,诊断时与本实例画像比对,
  不符的标「不适用本环境」;`evidence`(原文、照片、日志、报告,`kb.py attach` 存档);`synonyms`(现场的叫法);
- `aliases`:改过类型 / 迁移前的旧 ID,由脚本维护,不要手改。

## 版本范围写法

`505.2`(精确到给出的位数)· `505.*` · `>= 503` / `< 505.2` · `505.1–505.2`(闭区间)· `*`(全部),逗号分隔任一命中即算。
写法解析不了的按「适用性未知」处理,不猜。

## 反例:看起来相同、根因不同

「连接数打满」可能来自业务增量、锁阻塞,也可能是应用连接泄漏。同一现象在本行有过不同根因时:

- 每个案例的 `## 区分证据` 写清**凭什么判断是这个根因**(如「根会话 idle in transaction 且事务开始早于阻塞」vs「根会话是 DDL、落在批量窗口」);
- L1 专题导航把这些案例并排列在「候选原因」表里,诊断时模型先按区分证据取证,再引用对应案例的处置。
