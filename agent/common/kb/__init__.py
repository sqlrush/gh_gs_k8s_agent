"""common.kb —— 客户知识库的共享层,所有 skill 都从这里查库。

真相在 <kb>/ 下的文件;<kb>/index/store.sqlite(向量 + 词法)与 Neo4j(图,可选)是可重建的派生索引。
2026-10-23 起向量库是 SQLite + sqlite-vec(原高斯 DataVec / PG pgvector 向量库下线)。
本包只依赖标准库 + sqlite-vec(requirements.txt;没装时只是没有向量召回),Neo4j 走 HTTP。

模块分工:
    config      kb.yaml / OpenCode provider 端点 / 凭据名(密钥只在内存,repr 不泄露)
    text        中文二元组 + 标识符分词(在 Python 侧切好,不依赖引擎的分词器)
    store       存储层共用的数据形状
    bm25        词法打分(文件模式与 SQLite 索引同一套算式)
    store_sqlite  <kb>/index/store.sqlite:切块 + 词法倒排 + 向量(sqlite-vec)
    store_files 文件模式:还没建索引时直接在 <kb>/ 文件上检索
    store_graph Neo4j HTTP Query API
    embed       OpenAI 兼容 /v1/embeddings 客户端
    query       混合检索编排
    render      「客户知识库参照」小节
"""
