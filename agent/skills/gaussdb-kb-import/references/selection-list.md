# 选择列表(`inbox/<slug>/review.md`)——写库前的唯一闸门

`kb.py review <slug>` 校验 `candidates.json` 后生成。**你把它原样呈现给用户**,收集回答,再 `kb.py apply`。
你不改清单内容、不替用户做决定、不省略任何一项。

```markdown
# 导入确认:q1(1 案例 / 8 项待定 / 其中 2 条边无默认)
回复格式:`接受 1-9,12` / `拒绝 10` / `改 3: severity=S2` / `全部接受(边除外)`;边不答 = 保留为候选(不进「本行历史路径」)

## S2-20250224-CBST-偶现单条update慢
  1. [案例] S2-20250224-CBST-偶现单条update慢 · 结论强度 已确认 · 级别 S2   摘录:"表尾部空页回收持有8级锁…"   建议:接受
  2. [字段] severity = S2   建议:接受
  3. [字段] conclusion = 已确认   建议:接受
  4. [字段] primary_factor = autovacuum尾部空页回收持8级锁与DML互相cancel   摘录:"…"   建议:接受
  5. [实体] object cbst.cosp_asyn_task_dtl   摘录:"cbst.cosp_asyn_task_dtl"   建议:接受
  6. [实体] guc autovacuum_vacuum_threshold   摘录:"autovacuum_vacuum_threshold"   建议:接受
  7. [边] 单条update偶发秒级 —CAUSED_BY→ autovacuum尾部回收持8级锁   ⚠ 无默认   摘录:"持有8级锁"
  8. [边] autovacuum尾部回收持8级锁 —HANDLED_BY→ 表级调大autovacuum_vacuum_threshold   ⚠ 无默认   摘录:"调大autovacuum_vacuum_threshold"
```

## 各类条目的默认

| 类型 | 默认 | 说明 |
|---|---|---|
| `[案例]` `[字段]` `[实体]` | 接受(摘录回指成功时) | 用户说「全部接受(边除外)」即按默认 |
| `[实体] … ⚠ 摘录未回指` | 拒绝 | 模型给的摘录在原文里找不到——不入库 |
| 候选缺 `quotes.现场`,或结论 `已确认` 却缺 `quotes.primary_factor` | 整单 `[error]`,不进列表 | 补摘录,或把结论改成 `推测`,再跑 review |
| `[归一] 「X」≈ 某已有实体?` | 无默认 | 接受 = 记进 canonical.yaml,以后同名自动归一 |
| `[边]` | **无默认** | 接受 = confidence 1.0 进「本行历史路径」;拒绝 = 不入图;不答 = candidate(可检索,不进路径) |

## 把回答变成 apply 参数

用户说 → 你执行:

- 「全部接受,边也接受」→ `kb.py apply q1 --all-but-edges --accept 7,8`
- 「1-6 接受,7 接受,8 不对」→ `kb.py apply q1 --accept 1-7 --reject 8`
- 「级别改成 S1,其余按建议」→ `kb.py apply q1 --all-but-edges --edit 2:S1`
- 「先不管边」→ `kb.py apply q1 --all-but-edges`(边留候选)
- 用户不答就不要替他答;清单里有 `[error]` 时 apply 会拒绝,先修候选再 review。

`apply` 之后:`kb.py validate` → `kb.py index`。处理完的工单会从 `items/` 移走;`health` 会显示还剩几单。
