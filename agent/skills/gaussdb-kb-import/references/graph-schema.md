# 图(Neo4j)与三元组文件(`<kb>/graph/*.yaml`)

真相在文件,Neo4j 是 `kb.py index` 重建出来的派生索引。**只收客户专属、强类型、带出处、带置信度的关系;没有共现边。**

## 节点 kind → 标签

| kind | 标签 | 例 |
|---|---|---|
| `case` | Case | `case:S2-20250224-CBST-偶现单条update慢` |
| `symptom` | Symptom | 单条 update 偶发秒级(带 `signals`,节点向量放 SQLite 索引) |
| `rootcause` | RootCause | autovacuum 尾部回收持 8 级锁 |
| `action` | Action | 表级调大 autovacuum_vacuum_threshold |
| `object` / `guc` / `wait_event` / `error` / `component` | Object / Guc / WaitEvent / Error / Component | `object:cbst.cosp_asyn_task_dtl`、`guc:autovacuum_vacuum_threshold`、`wait_event:lwlock:walwritelock` |
| `clause` | Clause | `clause:GS-VAC-002` |

节点 id = canonical:先查 `graph/canonical.yaml` 别名表(`<id>: [别名…]`),查不到按 `kind:归一化名字` 生成;
标识符类(对象/GUC/等待事件)直接用小写原名。同一个东西的不同叫法必须落到同一个 id,否则图里是孤岛。

**最常犯、也最难自己发现的错:同一个现象写成两个节点。**
抽边时很容易把「案例 exhibits 的现象」按发现代码命名(`CACHE_LOW`),把因果链起点按描述命名
(`缓存命中率低,报表时段 blks_read 冲高`)。两个 id 一分家,这个案例就**永远走不到自己的 现象→根因→处置 链**——
不报错,只是路径小节写「无」。`kb.py validate` 会把这种现象逐个点名(「只有案例 exhibits 指向它,没有 caused_by 出边」),
修法是在 `canonical.yaml` 里把描述名作为别名并到代码名那个 id 上:

```yaml
symptom:cache_low: [缓存命中率低, "缓存命中率低,报表时段 blks_read 冲高"]
```

一条案例的 `exhibits` 目标与它的 `caused_by` 起点,**必须是同一个 id**。

## 关系 rel → 类型

`exhibits`(case→symptom)· `caused_by`(symptom→rootcause)· `handled_by`(rootcause→action)· `involves`(case→对象/GUC/等待事件/错误)·
`constrains`(clause→对象/GUC)· `references`(case→clause)· `depends_on`(对象→对象)。

## 三元组条目

```yaml
- src: {kind: symptom, name: 单条 update 偶发秒级}          # canonical 可省,脚本归一
  rel: caused_by
  dst: {kind: rootcause, name: autovacuum 尾部回收持 8 级锁}
  confidence: 1.0            # 用户在选择列表接受 = 1.0;模型自报封顶 0.9;结论强度映射 已确认 1.0 / 推测 0.6 / 待验证 0.3
  status: accepted           # accepted | candidate | rejected(rejected 不入图);省略 = accepted(手写的边视同用户确认)
  source: cases/S2-20250224-CBST-偶现单条update慢.md#判断   # 出处必填
  case: S2-20250224-CBST-偶现单条update慢
  valid_from: 2025-02-24     # 可空;valid_to 过期的边不进路径
```

边的身份是 `(src, rel, dst, source)`:同一条因果被两份材料各自佐证时是两条边,「N 案例支持」数的就是它。

## 路径查询(检索层用)

`(:Symptom)-[:CAUSED_BY]->(:RootCause)-[:HANDLED_BY]->(:Action)`,只走 **`status: accepted` 且 `confidence ≥ 1.0`**、
在生效期内的边,按 现象/根因/处置 三元组聚合,来源合并、案例去重计数。Neo4j 与图文件两种模式同一口径。

candidate 边永远不出现在「本行历史路径」里——**手工把它的 `confidence` 改成 1.0 也不会**:路径看的是 status,
置信度只是第二道条件。候选边要进路径,得**用户确认**后把 `status` 改成 `accepted`(confidence 记 1.0)——
这是用户的决定,不要替他改。
对象 → 条款(`constrains`)的确定性引用同样只认 accepted 边。

旧版 `kb.py index` 写进 Neo4j 的边没有 status 属性,一律按「不进路径」处理(拿不准就不进);
状态行会写「N 条边是旧版索引写的、不带 status」,重跑一次 `kb.py index`(图整体重建)即恢复。
