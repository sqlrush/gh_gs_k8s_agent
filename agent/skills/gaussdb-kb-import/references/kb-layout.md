# 知识库目录与条款格式规范(模型条款化时必须遵守)

## 目录结构(由 `kb.py ingest` 自动创建)

```
<kb>/                        # 与 skills/ 同级,随 skill 一起安装,重装不丢
  VERSION                    # 如 2026.07;规范大版本更新时手工递增
  L0-总索引.md               # L0:知识范围(八类各多少)、适用环境(画像)、专题入口 —— index 生成,勿手工编辑
  topics/                    # L1:每个专题一份导航(候选原因 + 区分证据 + 条目摘要)—— index 生成
    _taxonomy.yaml           #     专题定义(可编辑:kb.py taxonomy --init 写出默认)
  INDEX.md                   # 文件级地图(各目录一览),index 生成,勿手工编辑
  RULES.md                   # 现行条款逐条清单(兼容保留一个版本),index 生成,勿手工编辑
  CASES.md                   # 案例逐条清单(兼容保留一个版本),index 生成,勿手工编辑
  errata/                    # L2 公司内部规范 · 修正与例外(最高优先级):实战纠错、版本差异
  rules/                     # L2 公司内部规范 · 机器可判定条款(yaml,稳定 GS-* ID)—— 全部现行有效
  cases/                     # L2 历史案例排查思路
  runbooks/ defects/ baselines/ product/ wiki/ profiles/   # L2 六类类型化条目,见 knowledge-types.md
  guides/                    # 旧版语义指南(不再新写;kb.py migrate 归到上面六类)
  archive/                   # 已废止条款(yaml)—— 不在检索范围内,仅供 ID 追溯
  sources/                   # L3 原始文档快照(只读存档,不进上下文;kb.py evidence 按出处截取)
  evidence/                  # L3 照片、日志、报告(kb.py attach 存档)
  inbox/<slug>/              # ingest 产物:source.md + outline.md,条款化完删除;inbox/drafts/ 放类型化条目草稿
```

**`rules/` 里的条款一律是现行有效的。** 各 skill 用
`grep -rn "<关键词>" <kb>/errata <kb>/rules <kb>/guides` 检索知识库——`archive/` **有意**
不在这个范围内。这是废止机制的全部原理,见下方「换版」。

## 规则 ID 规范(永不复用,只增不改)

- 格式:`GS-<域>-NNN`,域为 2-4 位大写字母,NNN 三位数字从 001 起。
- 建议域:`NAM` 命名 / `DDL` 建表 / `IDX` 索引 / `DML` 增删改查 / `PRC` 存储过程 /
  `GUC` 参数 / `SEC` 安全 / `MIG` 迁移兼容。新域按需自定,保持 2-4 位大写。
- 分配新 ID 前先 `kb.py search GS-<域>- --include-archived` 查当前最大号
  (**必须带 `--include-archived`**,否则会漏掉 archive/ 里已废止的号段而重复分配);
  **废止条款保留 ID 空洞,绝不把旧 ID 分配给新条款**(报告里引用过的 ID 必须永远可追溯)。
  `validate` 会跨 `rules/` 与 `archive/` 查重,复用旧 ID 会被拦下。

## 换版:客户的规范升版了怎么办

**废止一条条款是「移走」,不是「打个标记」,更不是「删掉」。**

为什么必须移走:各 skill 用 `grep -rn` 检索,而 grep **只输出命中行**。一条留在 `rules/` 里、
仅仅标了 `status: deprecated` 的条款,模型搜「外键」时看到的是
`rules/table.yaml:12: rule: 禁止使用外键约束` —— 它**看不到 status 那一行**,照样会按
已经作废的规范判客户违规。所以物理隔离到 `archive/` 是唯一可靠的办法。

为什么不能删:历史报告引用过 `GS-TBL-002`,删掉就永远查不到它当初说了什么。

**换版流程(`ingest` 检测到 `rules/` 非空时会提示):**

1. `ingest` 新版规范 → `inbox/<slug>/`。
2. **先读 `INDEX.md`**,把新版原文与库里现有条款**逐条比对**,给用户看一张表:
   `ID | 一句话 | 新增 / 沿用 / 修改 / 废止`。
3. 用户确认后:
   - **新增** → 分配新 ID,写进 `rules/`;
   - **修改** → 原地改 `rules/` 里那条(ID 不变,更新 `source` 指向新版小节);
   - **废止** → 整条**移进 `archive/<域>.yaml`**,补 `status: deprecated` 与 `superseded_by`;
   - **沿用** → 不动(可更新 `source` 指向新版对应小节)。
4. 递增 `VERSION`,跑 `index` + `validate`。

`validate` 会**双向**校验(两个方向都是静默失效,所以都是 `[error]`):

- 标了 `status: deprecated` 却还留在 `rules/` → 各 skill 的 grep 照样命中它;
- 放进了 `archive/` 却没标 `status: deprecated` → 一条现行条款被静默地移出了检索范围。

## rules/*.yaml 条款 schema(validate 会强校验)

```yaml
# 索引规范(文件首行注释会进 INDEX)
- id: GS-IDX-003            # 必填,GS-<域>-NNN
  severity: error           # 必填,error | warn | info
  check: deterministic      # 必填,deterministic(脚本可判)| advisory(模型判)
  rule: 单个索引列数不超过 4 列    # 必填,一句话条款
  rationale: 超过后选择率增益递减,写放大显著   # 建议
  criteria: ""              # advisory 必须给:判定依据(看什么证据、怎么判)
  keywords: [组合索引, 联合索引, 复合索引, composite index]   # 强烈建议:3-6 个同义词
                            # (客户用语 + 通用叫法 + 英文),离线做语义扩展,
                            # 运行时 search/grep 纯字面匹配即可命中
  applies: [opengauss>=3.0, gaussdb-a]        # 建议
  source: 《XX规范》v5 §4.2.1                  # 建议(validate 会 warn)
  status: active            # 可选,active(默认)| deprecated
                            # 省略 = active。存量条款没这个字段,一律视为现行有效。
```

已废止的条款(**只能出现在 `archive/`**):

```yaml
- id: GS-TBL-002            # ID 保持不变,永不复用
  severity: error
  check: deterministic
  rule: 禁止使用外键约束      # 原文照留,不要改写
  source: 《XX规范》v2.1 §2.2  # 指回它当初所在的那一版原文
  status: deprecated        # 必填(archive/ 里不标会被 validate 判 error)
  superseded_by: 《XX规范》v2.2 已删除该条,无替代条款   # 建议:被哪条取代 / 为何废止
                            # 也可写成另一条 ID,如 GS-TBL-007
```

## guides/*.md frontmatter schema

```yaml
---
id: guide-index-design       # 必填,库内唯一
description: 索引设计:高基数判断、组合索引列序、局部索引取舍   # 必填(进 INDEX)
scope: [sqltune, sqlreview, explain]   # 建议:主要消费的 skill
source: 《XX规范》v5 §4 + errata/2026-07-xx.md   # 建议
---
```

## errata/*.md 格式

文件名 `YYYY-MM-DD-<slug>.md`;正文首行为一句话结论(会进 INDEX),
必须写明:修正/推翻哪条(规则 ID 或 guide 小节)、环境(版本/部署形态)、依据。

## 条款化判定口径

- **能写成"看到 X 即违规"的 → rules**;拿不准能否确定性判定的,一律 `check: advisory`。
- 讲"怎么设计/怎么权衡/什么场景选什么"的 → guides。
- 与库内既有条款矛盾、或明确是版本特例的 → errata(并在 errata 里引用被修正的 ID)。
- 每条 rules/guides 都必须能指回 `inbox/<slug>/source.md` 的具体小节(填进 source 字段);
  指不回去的内容**不要入库**——知识库只收有出处的规范,不收模型的自由发挥。

## 金丝雀条款(建议)

入库时埋 2-3 条与通用 PostgreSQL 常识**故意相反**的真实条款并记下 ID,
定期用它们提问验证各 skill 是否真按知识库作答(失守 = 契约失效,重查 SKILL.md 契约块)。
