# 安装与连接(OpenGauss/GaussDB)

本工具以 Python 脚本运行。

## 安装

```bash
git clone https://github.com/sqlrush/opencode_skill
cd opencode_skill && python3 -m pip install -r requirements.txt   # psycopg2, cryptography, PyYAML
python3 skills/gaussdb-sqltune/scripts/sqltune.py -h
```

装进 OpenCode:见 `docs/INSTALL-opencode.md`。

## 添加连接

连接存在 `../common/`(共享、字节兼容的存储),用 `-c <name>` 选择。建连见 `docs/INSTALL-opencode.md` §1:

- 连接配置目录由 `GSDB_HOME` 指定，（默认配置在 `../common`下）
- 复用已有连接:`cat "$GSDB_HOME/config.yaml"`(只看名字,无密码)
- 手工创建:写 `$GSDB_HOME/config.yaml`,再用仓库的 `common.save_secret` 加密密码(写入 `$GSDB_HOME/credentials/<name>.enc`,AES-256-GCM)
- CI / 一次性:设 `GSDB_PASSWORD`(旧 `GDAA_PASSWORD` 仍兼容;用 `GSDB_HOME` 可换存储位置)

GaussDB:连接项里设 `type: gaussdb`。

验证连通(只读):

```bash
python3 skills/gaussdb-sqltune/scripts/sqltune.py -c og-prod --sql-stdin <<'SQL'
SELECT 1
SQL
```

## 监控账号最小权限

```sql
-- 用管理员执行;OG:monadmin 即可覆盖 dbe_perf
ALTER USER tuner MONADMIN;
-- 或显式授权:
GRANT USAGE ON SCHEMA dbe_perf TO tuner;
GRANT SELECT ON ALL TABLES IN SCHEMA dbe_perf TO tuner;
```

## 语句跟踪所需 GUC

```sql
ALTER SYSTEM SET enable_stmt_track = on;        -- statement_history 行
ALTER SYSTEM SET track_stmt_parameter = on;     -- 字面 SQL(否则归一化)
```

## 症状 → 处理

| 症状                                     | 处理 |
|----------------------------------------|---|
| 退出码 2 / 「该能力在白名单模型下不可用」        | 该连接的 driver 是 `grmp`(走中间件),中间件只执行预注册脚本、且每次调用独立连接。sqltune/verify 要 EXPLAIN 用户临时给的任意 SQL,并在同一会话里做 hypopg 虚拟索引验证,两件事都做不到。**改用 driver 为 `psycopg2` 的连接**;若客户环境只有中间件通道,如实说明本 skill 当前无此能力并停止,不要凭表/索引/统计信息编调优结论。只看 SQL 原文或慢 SQL 清单可改用 gaussdb-sqlfetch / gaussdb-topsql / gaussdb-slowsql / gaussdb-health(这些已全量走中间件) |
| 退出码 2 / connection refused             | 查 host/port/防火墙;用脚本跑 `SELECT 1` 验证 |
| 退出码 2 / password authentication        | 重建凭据(见「添加连接」),或设 `GSDB_PASSWORD`(旧 `GDAA_PASSWORD` 仍兼容) |
| 退出码 3 / permission denied for dbe_perf | 授 monadmin(上面 SQL) |
| gaussdb-sqlfetch 查不到                   | 开 `enable_stmt_track`,等有流量后重试 |
| gaussdb-sqlfetch 返回归一化 SQL             | 开 `track_stmt_parameter`;或用 `--bind` 传真实值 |
| 退出码 4 / syntax or object error         | 检查替换后的占位符值与 SQL 里的对象名 |
| 退出码 5 / timeout                        | 调大 `--timeout`,或错峰调优 |
