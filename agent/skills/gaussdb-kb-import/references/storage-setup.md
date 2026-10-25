# 存储接入(`<kb>/kb.yaml` + 凭据)

**向量库不用配。** 2026-10-23 起,向量 + 词法索引是知识库目录里的一个文件 `<kb>/index/store.sqlite`
(SQLite + sqlite-vec),`kb.py index` 建它、各 skill 只读打开它;原来的高斯 DataVec / PG pgvector 向量库下线,
不再需要知识库专用的数据库实例、库用户与口令。

- 还没 `index`(或索引文件打不开):脚本自动进入**文件模式**——词法检索直接在 `<kb>/` 的 rules / cases / guides / errata 上做,
  状态行写「模式:文件(原因)」。导入、选择列表、query、各 skill 的小节格式全都一样,少的只有向量语义召回。
- 索引是可重建的派生物,真相在 `<kb>/` 的文件里:坏了 `kb.py index --rebuild` 就回来。
- 写索引时在临时副本上改、改完原子替换,读的一方任何时刻看到的都是完整的一份;只有持知识库写锁的 `index` 会写。
- 向量检索要 Python 能加载 sqlite-vec(`requirements.txt` 里锁了 `sqlite-vec==0.1.9`)。加载不了时(没装、或 Python 编译时
  不许加载扩展,如 macOS 自带的 python)状态行写「向量:未启用(原因)」,词法与图照常。

图库 **Neo4j** 仍是可选的(社区版即可):配了且连得上就用,否则「本行历史路径」从 `graph/*.yaml` 里客户确认过的边走出来。
它是知识库**专用**实例,不是被管的业务库。

## kb.yaml(只有连接元数据与凭据名,**绝不写口令**)

```yaml
store:
  graph: {url: http://10.0.0.9:7474, user: neo4j, credential: kb-graph, database: neo4j}   # 可选
embeddings:
  source: opencode            # opencode = 复用 OpenCode 配置的 provider 端点;url = 显式;none = 不开向量
  model: bge-m3               # 该端点上挂的嵌入模型名
  dims: 1024
  # base_url: http://127.0.0.1:11434/v1   # source: url 时
  # api_key_env: EMBED_API_KEY            # source: url 时,可选
thresholds: {}                # 分类型分数阈值,默认即可
```

`kb.yaml` 里出现 `password` / `apikey` / `token` 直接报错。Neo4j 的口令用加密凭据:

```bash
python3 -m common.credential_cli set kb-graph    # 交互输入,AES-256-GCM 落 $GSDB_HOME/credentials/kb-graph.enc
```

从旧版本升级:`kb.yaml` 里留着的 `store.pg` 段不会报错,`index` / `health` 会提示可以删掉;升级后跑一次 `kb.py index`
建出 `index/store.sqlite`(之前检索一直走文件模式,不会中断)。

## 步骤

```bash
python3 {baseDir}/scripts/kb.py setup      # 探测:索引文件位置、向量能否用(sqlite-vec)、Neo4j 版本(配了就建约束)
python3 {baseDir}/scripts/kb.py index      # 文件 → index/store.sqlite(+ Neo4j)
python3 {baseDir}/scripts/kb.py health     # 状态行 + 覆盖率 + 待处理 + 缺口清单
```

## 降级即发现

- sqlite-vec 加载不了:`kb_meta.vector_engine=none`,状态行写「向量:未启用(原因)」;
- 配了 embedding 但覆盖率不满:`index` 退出码 2;端点通了再跑一次 `index` 就补齐(先试一条,通了才逐批补),端点不稳时 `index --fill-missing` 强制逐批重试;状态行写覆盖百分比;
- 没配 embedding:只告警,不算失败,状态行写「未启用(kb.yaml 未配 embeddings)」;
- Neo4j 没配或不可达:index 只写 SQLite 索引并告警,检索时路径改走 `graph/*.yaml`,状态行写「模式:向量库+图文件 · 图:图文件 N 条已确认边(原因)」;
- 还没 index / 索引文件打不开 / 格式旧了:整体退到文件模式,状态行写「模式:文件(原因)· 向量:未启用(文件模式无向量)」,skill 本身照常;
- 索引写不进去(目录只读、盘满):文件索引照建,`index` 退出码 2 并写明原因,检索退到文件模式;
- 只有 `<kb>/` 目录不存在或 `kb.yaml` 无效才是「知识库未接入(原因)」。

## embedding 端点

`source: opencode` 读 `~/.config/opencode/opencode.jsonc` 里当前 `model` 所属 provider 的 `options.baseURL` 与 key,
向 `<baseURL>/embeddings` 请求 `embeddings.model`。前提是这个 provider 上确实挂了嵌入模型(Ollama / vLLM / Xinference
都能顺道挂);纯聊天 API 没有,就用 `source: url` 指一个单独的嵌入服务,或 `source: none`。
索引侧超时 30 s/批、查询侧 1.5 s,超了本次不用向量,状态行注明。

- **后配上 embedding**:改好 `kb.yaml` 再跑一次 `kb.py index` 就会给已有的块补向量(先试一条,端点通了才逐批补);
  端点不稳时 `index --fill-missing` 强制逐批重试。
- **Ollama 的冷启动**:模型闲置 5 分钟会被卸载,下一次查询要先加载 —— 2026-10-24 测试环境实测 bge-m3 第一次加载 1.1–1.4 秒,
  贴着查询侧 1.5 秒的超时,超了这一次只用词法 + 图。用 Ollama 时设 `OLLAMA_KEEP_ALIVE=-1`(常驻),
  或在 kb.yaml 里把 `query_timeout_s` 放宽到 3。
- **容器部署里的 `source: opencode`**:opencode 的配置在 `OPENCODE_CONFIG`,模型地址是本 Pod 的凭据代理,
  Key 由代理加上;嵌入请求同样经代理转到模型服务的 `/embeddings`。前提是模型服务本身有嵌入接口 ——
  DeepSeek 等纯聊天 API 回 404,这时用 `source: url` 指单独的嵌入服务。
