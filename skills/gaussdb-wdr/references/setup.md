# 安装与常见症状（wdr）

`wdr` skill 用 Python 脚本（`scripts/wdr.py`）只读取数与渲染，绝不旁路。

## 依赖

```bash
python3 -m pip install psycopg2 cryptography PyYAML
python3 {baseDir}/scripts/wdr.py -h     # 验证脚本可运行
```

连接配置目录由 `GSDB_HOME` 指定：`$GSDB_HOME/config.yaml`（无密码），口令经本机密钥 AES-256-GCM 加密存于 `$GSDB_HOME/credentials/*.enc`，由脚本解密；也可用环境变量 `GSDB_PASSWORD`（旧 `GDAA_PASSWORD` 仍兼容）覆盖。`-c <NAME>` 用 `config.yaml` 里 `connections[].name`。

## 症状对照

- `WDR 未开启（enable_wdr_snapshot=off）`：由 DBA 在 DB 侧 `ALTER SYSTEM SET enable_wdr_snapshot=on`（需 reload/重启），等待自动快照或 `SELECT create_wdr_snapshot();`。本工具不代为开启/创建。
- `可用快照不足`：需至少两个快照围出窗口；等待下一自动快照间隔或由 DBA 手动创建。
- `generate_wdr_report 不可用`：实例版本/权限不支持原生 WDR；自算 delta 仍可用，原生留底显示降级备注即可（不影响确定性发现）。
- 某维度降级 `relation ... does not exist` 或 `column ... does not exist`：该版本快照视图结构不同（如 lite 缺某视图）；属正常降级，如实说明、不臆测。
- `permission denied`：WDR 快照视图通常需 `monadmin` 或具相应权限的角色；用具备权限的账号连接。
- `ModuleNotFoundError: psycopg2` 等：缺 Python 依赖，按上「依赖」安装。
