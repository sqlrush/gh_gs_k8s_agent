# proctune / procinfo 安装与排错

## 一、前置条件

`gaussdb-proctune` 由本仓库的 Python 脚本运行。

- Python ≥ 3.9，并装好依赖：`python3 -m pip install -r requirements.txt`（`psycopg2` / `cryptography` / `PyYAML`）。
- 预检入口脚本 `proctune.py -h`（实际路径见 `docs/INSTALL-opencode.md` 或安装目录）。报缺依赖时按上面装齐。
- 安装到 OpenCode：见 `docs/INSTALL-opencode.md`。

## 二、配置连接

连接信息复用 `$GSDB_HOME`（与 SQL 优化族共用的连接+凭据存储），通过 `-c <name>` 选连接。新建 / 查看连接见 `docs/INSTALL-opencode.md` 第 1 步：

- 连接配置目录由 `GSDB_HOME` 指定。
- 已有连接：`cat "$GSDB_HOME/config.yaml"` 看 name 列表（不含密码）。
- 手工新建：写 `$GSDB_HOME/config.yaml` + 用本仓库 `common` 的 `save_secret` 加密口令（落 `$GSDB_HOME/credentials/<name>.enc`）。
- 非交互 / CI 场景用环境变量 `GSDB_PASSWORD`（旧 `GDAA_PASSWORD` 仍兼容）覆盖存储口令；`GSDB_HOME` 覆盖根目录。

口令以 AES-256-GCM 加密存储；`gaussdb-proctune` 全程只用 `-c <name>`，不构造含密码的连接串。验证连接可用：跑一次 `proctune.py collect -c <name> <schema.proc>`，能取到 `## Procedure Source` 即连通。

## 三、开启运行时归因（可选但推荐）

`## Runtime Attribution` 需要实例采集**嵌套语句**统计。未开启时 `gaussdb-proctune` 自动降级为纯静态分析（命令不失败），但拿不到「哪条 embedded SQL 真正耗时」的真实排序。

- 把 `track_stmt_stat_level` 调到捕获嵌套层级（具体取值见实例文档），重采 `proctune.py collect` 即可获得真实 calls/avg/total。
- 这是只读统计，不改业务行为。

## 四、症状对照表

| 症状 | 可能原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'psycopg2'` | 缺 Python 依赖 | `python3 -m pip install -r requirements.txt` |
| `ModuleNotFoundError: No module named 'common'` | 没用安装脚本（漏拷 `common/`） | 重跑 `install-opencode.sh` |
| `no connection named 'xxx'` | 连接名未配置 | 见「二、配置连接」，核对 `$GSDB_HOME/config.yaml` |
| 连接失败（退出码 2） | host/port/凭据错或库不可达 | 跑一次 `proctune.py collect` 验证；确认网络与账号；或用 `GSDB_PASSWORD`（旧 `GDAA_PASSWORD` 仍兼容） |
| 权限不足（退出码 3） | 账号缺 `pg_get_functiondef` / `dbe_perf` / `gs_index_advise` / hypopg 权限 | 用有相应读取/调优权限的账号 |
| `## Runtime Attribution` 为空或标「不可用」 | 未开 `track_stmt_stat_level` 捕获嵌套语句 | 见「三、开启运行时归因」；不开则按纯静态分析进行 |
| `## Verified Index Candidates` 显示无候选 | gs_index_advise 未发现，或合成值下无收益 | 用 `--bind <var=value>` 传真实值重验；或仅作未验证思路呈现 |
| 索引验证报「不可用」 | 实例未启用 hypopg / `enable_hypo_index` | 确认 OpenGauss 版本支持并启用；不支持则索引验证降级，命令仍成功 |
| 大量游标进 `## Skipped Cursors` | FOR UPDATE / 动态 / 参数化 / 包内 / REF CURSOR | 属预期：v1 仅自动改只读基础游标，其余转建议 |
| 语句超时（退出码 5） | 采证据/验证超过 `--timeout` | 增大 `--timeout`，或检查目标对象规模 |

## 五、与 sqltune 的关系

`gaussdb-proctune` 对游标 SELECT 的优化复用 gaussdb-sqltune 同一套机器（`Collect` / `VerifyIndexes` / `verify.py`）。若单独一条 SQL 的调优诉求，直接用 `/gaussdb-sqltune` 更直接；`gaussdb-proctune` 面向「过程内的游标 + 过程结构」整体分析。
