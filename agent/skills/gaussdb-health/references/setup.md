# 安装与故障对照（gaussdb-health）

`gaussdb-health` skill 用 Python 脚本（`scripts/health.py`）只读取数与诊断，绝不旁路。

## 依赖

```bash
python3 -m pip install psycopg2 cryptography PyYAML
python3 {baseDir}/scripts/health.py -h     # 验证脚本可运行
```

## 配置连接

连接配置目录由 `GSDB_HOME` 指定（任意名/路径，默认 `$GSDB_HOME`，旧 `GDAA_HOME` 仍兼容）：`$GSDB_HOME/config.yaml`（无密码），口令经本机密钥 AES-256-GCM 加密存于 `$GSDB_HOME/credentials/*.enc`，由脚本解密；也可用环境变量 `GSDB_PASSWORD`（旧 `GDAA_PASSWORD` 仍兼容）覆盖。`-c <NAME>` 用 `config.yaml` 里 `connections[].name` 字段。

`$GSDB_HOME/config.yaml` 示例：

```yaml
connections:
  - name: og
    type: opengauss        # 或 gaussdb
    host: 127.0.0.1
    port: 5432
    database: postgres
    user: dbuser
```

## 故障对照

| 症状 | 含义 | 处理 |
|---|---|---|
| `ModuleNotFoundError: psycopg2` 等 | 缺 Python 依赖 | `python3 -m pip install psycopg2 cryptography PyYAML` |
| 退出码 2 / `connect ...` 报错 | 连不上（主机/端口/口令/库名错） | 核对 `$GSDB_HOME/config.yaml` 与网络；口令用 `GSDB_PASSWORD`（旧 `GDAA_PASSWORD` 仍兼容）临时覆盖验证 |
| 退出码 1 / `insufficient privilege` | 采集账号缺系统视图权限 | 用有 `pg_stat_*` / `dbe_perf` / `pg_thread_wait_status` 读权限的账号；缺权限的维度会在 `## Collection Notes` 标降级 |
| 退出码 1 / `statement timeout` | 采集超时 | `--timeout <秒>` 调大；或先 `--include` 只采关键维度 |
| 某维度 `## Collection Notes` 标"降级" | 该实例缺对应视图（版本差异）或无权限 | 如实告知该维不可用，其余维度仍出报告；不要臆测被降级维度的结论 |

## 规则提醒

- gaussdb-health 默认只读，不接受任何写操作，绝不执行 kill / VACUUM / DML。
- 取数失败时如实说明，绝不改用 psql / gsql 或自己写 Python 自行连库。
