# 后端镜像的环境契约（给平台）

镜像：`gaussdb-agent-runtime`（每用户一个 Pod）、`gaussdb-agent-kb-import`（知识库管理员）。两者的接口相同，差异见末尾。

## 环境变量

| 变量 | 必填 | 值 | 来源 | 说明 |
|---|---|---|---|---|
| `GSDB_USER_ID` | 是 | 工号 | 平台 | 写进 findings 信封与 health 报告的「执行人」；配了 `GRMP_USER_ID_*` 时也是发给中间件的那个工号（**同一个来源**，两处取值不一致比不传更糟） |
| `POD_NAME` | 建议 | Pod 名 | downward API `metadata.name` | `.owner` 独占标记里的持有者；缺省用 `hostname` |
| `GRMP_API_HOST` | runtime 是 | 中间件主机名或 IP | ConfigMap | 每次启动重新渲染进 `config.yaml`，改地址重建 Pod 即生效 |
| `GRMP_API_PORT` | 否 | 默认 `8080` | ConfigMap | |
| `GRMP_PATH_PREFIX` | runtime 是 | 中间件接口路径前缀（中间件团队提供，以 `/` 开头、只写路径），技能请求 `<前缀>/common-operations` 与 `<前缀>/common-operations/invoke` | ConfigMap | 2026-10-26 起由部署配置，代码里没有默认值；没配时登录数据库与凭据代理都直接报「GRMP_PATH_PREFIX 没配」，不猜 |
| `GRMP_AUTH_TOKEN` | runtime 是 | 本人令牌 | Secret，按人 | **只注入凭据代理容器 `credproxy`**（2026-10-23，见下文「凭据代理」），opencode 容器里没有；镜像与 NAS 都不落盘 |
| `GRMP_APPKEY` | 中间件开了签名校验时是 | 与中间件约定的应用名（整个智能体一个） | ConfigMap | 2026-09-18 中间件加固：请求头再带 Appkey / Timestamp / Signature；为空 = 不签名 |
| `GRMP_SIGN_MODE` | 否 | `full`（默认，三个头）/ `headers-only`（只 Appkey + Timestamp） | ConfigMap | 2026-09-20 客户过渡期：密钥审批未下来，先只加 Appkey 与 Timestamp。`headers-only` 时不读私钥。**拿不到私钥不会自动降级**——降级必须显式配置，否则生产上 Secret 挂载失败会悄悄变成不签名 |
| `GRMP_SM2_PRIVATE_KEY` | `GRMP_APPKEY` 非空且 `GRMP_SIGN_MODE=full` 时是 | 该 Appkey 的 SM2 私钥（64 位 hex 或 PEM），客户签发 | Secret `grmp-sm2`，全体 runtime 共用，**只注入 `credproxy` 容器** | 模板里 `optional: true`；该配而拿不到时代理启动日志写明，每个中间件请求都报「中间件签名配置有误」并提示可改用 `headers-only`（模型照常可用）。kb-import 不需要 |
| `GRMP_SIGN_USER_ID` / `GRMP_SIGN_TIMESTAMP` / `GRMP_SIGN_FORMAT` / `GRMP_SIGN_ENCODING` / `GRMP_SIGN_PAYLOAD` | 否 | 签名旋钮：userId（默认国标 `1234567812345678`，OpenSSL 默认是空串）、`ms`/`s`、`raw`/`der`、`hex`/`base64`、原文模板（默认 `{path}+{timestamp}`） | ConfigMap | 按中间件口径填；值不认识时建连接即报错 |
| `GRMP_USER_ID_HEADER` / `GRMP_USER_ID_PARAM` | 中间件要收工号时填其一 | 请求头名（如 `X-User-Id`）/ 报文顶层键名（如 `userId`） | ConfigMap | 2026-09-20 客户确认要显式收调用人工号，但字段名与位置未给，故两处都做成开关。值取自 `GSDB_USER_ID`。两个都为空 = 不发工号（行为与加此特性前一致）。**填了字段名而 `GSDB_USER_ID` 为空时建连接即报错，绝不发空工号** |
| `MODEL_BASE_URL` | 是 | 模型服务的 OpenAI 兼容地址（含 `/v1`） | ConfigMap | |
| `MODEL_API_KEY` | 是 | 模型服务密钥 | Secret `model-api`，**只注入 `credproxy` 容器** | 不再写进 `opencode.json`：那里的 `apiKey` 是占位 `credproxy`，`baseURL` 是 `MODEL_PROXY_URL`，真 Key 由代理在转发时加上 |
| `MODEL_ID` | 是 | 模型 id | ConfigMap | opencode 默认模型 = `<MODEL_PROVIDER_NAME>/<MODEL_ID>` |
| `MODEL_PROVIDER_NAME` | 否 | 默认 `model-service` | ConfigMap | |
| `MODEL_INPUT_MODALITIES` | 要看图时是 | 模型能接收的输入，逗号分隔：`text,image`（可选项 `text` / `image` / `audio` / `video` / `pdf`，`text` 自动带上） | ConfigMap | 不设 = 纯文本：opencode 的 read 不把图片交给模型，报「this model does not support image input」——知识库导入读照片 / 截图要设 `text,image`。只在模型服务确实支持图片时设；写了不认识的项 Pod 启动即失败并列出可选项 |
| `KB_CLIP_ORIGINS` | 要用网页剪藏时是 | 允许剪藏的内网网站，`协议://主机[:端口]`，逗号分隔 | ConfigMap（agent-config） | 空 = 关闭剪藏。网关（映射成 `GATEWAY_KB_CLIP_ORIGINS`，剪藏页只收这些网站交来的内容）与导入容器（`kb.py clip status`）读同一份；格式写错网关拒绝启动并指出哪一项 |
| `KB_CLIP_MAX_MB` | 否 | 单次剪藏（网页 + 页内图片）总大小上限，默认 `30`，最大 `45` | ConfigMap（agent-config） | 网关映射成 `GATEWAY_KB_CLIP_MAX_MB`；导入容器的剪藏接收按同一个数校验 |
| `KB_CLIP_RECEIVER` | 模板内置 | 导入容器 `1` | 模板 | entrypoint 据此在 4095 拉起剪藏接收（`podctl serve-clip`，`docker/clipd.py`）；诊断容器没有这个端口 |
| `MODEL_PROXY_URL` | 模板内置 | `http://127.0.0.1:4099/model` | 模板 | opencode 经它访问模型（凭据代理）。不设时退回旧做法：`MODEL_BASE_URL` + `MODEL_API_KEY` 直接写进 `opencode.json`（只给不用 K8s 的单机演示） |
| `GRMP_PROXY_SOCKET` | 模板内置 | `/run/credproxy/credproxy.sock` | 模板 | 技能脚本经这个套接字调中间件，不再要求本容器有 `GRMP_AUTH_TOKEN` |
| `AGENT_ROLE` | 镜像内置 | runtime 镜像 `runtime`（默认）/ kb-import 镜像 `kb-import` | 镜像 | 决定 opencode 的工具权限（见下文「工具权限」）。平台不要覆盖 |
| `OPENCODE_SERVER_PASSWORD` | 建议 | 本 Pod 随机口令 | 网关生成 | opencode 自带的 HTTP 基本认证；配套 `OPENCODE_SERVER_USERNAME`（默认 `opencode`） |
| `OPENCODE_PORT` | 否 | 默认 `4096` | — | |
| `NAS_ME` / `NAS_KB` | 否 | 默认 `/nas/me`、`/nas/kb` | — | 挂载点变了才需要改 |
| `GSDB_KB_INBOX` | kb-import 内置 | `/nas/kb/inbox/uploads` | 镜像 | 用户上传待导入文件的收件目录 |

entrypoint 自己设置、平台**不要**覆盖的：`XDG_DATA_HOME=$NAS_ME/xdg-data`、`XDG_STATE_HOME=$NAS_ME/xdg-state`、`XDG_CONFIG_HOME=/data/config`（启动时从镜像 `/opt/agent/config` 复制，因为 opencode 加载配置要往这里写 `.gitignore`）、`XDG_CACHE_HOME=/data/cache`（同理，从 `/opt/agent/cache` 复制）、`GSDB_HOME=$NAS_ME/gdaa`、`GSDB_KB_DIR=$NAS_KB`、`HOME=/data/home`、`OPENCODE_CONFIG=/data/oc/opencode.json`。

## 挂载

| 容器路径 | 来源 | 权限 | 内容 |
|---|---|---|---|
| `/nas/me` | NAS PVC，`subPath: users/<工号>`（kb-import 用 `admins/<工号>`） | 读写 | `xdg-data/opencode/opencode.db`（会话、对话、压缩摘要）、`xdg-state/`、`workspace/`（上传文件、报告）、`gdaa/`（登录会话、每次启动重生成的 `config.yaml`）、`backup/`（退出备份，3 份）、`.owner` |
| `/nas/kb` | NAS PVC，`subPath: kb` | runtime **只读**；kb-import 读写 | 知识库文件；`inbox/uploads/` 是管理员上传待导入文件的收件目录（网关写入） |

用户上传的文件由**网关**直接写进 NAS（普通用户 `users/<工号>/workspace/uploads/`，管理员另有 `kb/inbox/uploads/`），Pod 只读到结果；镜像不提供上传接口。
| `/data` | emptyDir | 读写 | 渲染出的配置、`HOME`、`env.sh`；随 Pod 消失 |

NAS 目录对 uid 1000 可写（镜像以 `agent`，uid 1000 运行）。NFS 要求见 spec §4：`nfsvers=4.1`、`hard`、`local_lock=all`。

## 端口与探针

- 容器端口 `4096`（HTTP，`opencode serve`）。
- 就绪 / 存活：`GET /global/health` → `{"healthy":true,"version":"1.18.27"}`。镜像自带 `HEALTHCHECK` 用同一路径。

## 停机

`terminationGracePeriodSeconds: 60`。收到 SIGTERM 后 entrypoint 依次：等 opencode 退出 → `PRAGMA wal_checkpoint(TRUNCATE)` → 复制 `opencode.db` 到 `backup/`（保留 3 份）→ 删 `.owner`。网关先摘流量再删 Pod（`preStop: sleep 5`）。

## 启动时发生什么（日志里能看到）

1. 建目录；渲染 `/data/oc/opencode.json` 与 `$GSDB_HOME/config.yaml`（配置不是状态，每次重生成）。
2. `.owner` 独占：另一个 Pod 90 秒内刷新过标记 → 最多等 55 秒 → 仍被占则退出 1，不打开 db。
3. `opencode.db` 完整性自检：损坏 → 改名留证 → 从 `backup/` 最新一份恢复；没有备份 → 空库启动。每种情况都写日志，不静默。
4. 删 7 天前的 `log/*.log`。
5. 首次启动把 `/nas/me/workspace` 初始化成 git 仓库（空提交），opencode 以此识别「项目」，Web 界面按项目列会话。
6. `opencode serve --hostname 0.0.0.0 --port 4096`，工作目录 `/nas/me/workspace`。`serve` 自带 Web 界面（`/`）与 HTTP API（`/doc` 是 OpenAPI）。

## 凭据代理（2026-10-23）

opencode 容器里跑的是会执行命令的模型。凭据放在它的环境变量里，`env` 一下就能看到；以前挡在前面的只有提示词。
现在中间件令牌、SM2 私钥、模型 Key 都只注入同一个 Pod 里的第二个容器 `credproxy`（同一个镜像，`podctl serve-credproxy`，只跑我方代码）：

```
技能脚本 ──/run/credproxy/credproxy.sock──▶ credproxy ──加 auth、Appkey / Timestamp / Signature、本人工号──▶ 中间件
opencode ──http://127.0.0.1:4099/model───▶ credproxy ──加 Authorization: Bearer <模型 Key>──────────────▶ 模型服务
```

- **只往自己配置的地方转发**：中间件地址取 `credproxy` 容器自己的 `GRMP_API_HOST` / `GRMP_API_PORT`（可选 `GRMP_API_SCHEME`），模型取 `MODEL_BASE_URL`；
  请求里的 Host 一概不认，也不走环境变量里的 HTTP 代理。中间件只转发诊断接口那两个路径。
- **工号按本 Pod 的人强制写入**（`GRMP_USER_ID_*` 配了的位置），请求里自带的工号会被覆盖。
- 两个容器之间只共享一个 1 MiB 的 emptyDir（放套接字，opencode 容器只读挂载）；`credproxy` 不挂 NAS、不挂 `/data`，没有对外端口。
- 模型直接连代理能做的，也只是「以本人身份调白名单接口」「用模型」，与它经技能、经 opencode 本来就能做的一样，只是拿不到凭据本身。

固定池化下代理在管家容器里，每个槽位一个套接字，见 `docs/固定池化手册.md`。

## 工具权限（2026-10-23）

技能包 AGENTS.md 开头的 permission 是作为提示词装进去的，opencode 不执行。现在由 `podctl` 渲染进 `opencode.json`（opencode 真正执行的那份）：

| 工具 | runtime | kb-import |
|---|---|---|
| `edit`（含 write） | 拒绝 | 放行（`gaussdb-kb-init` 用 write 写 `draft/NNN.json`；只有知识库管理员进得来） |
| `webfetch` / `websearch` | 拒绝 | 拒绝 |
| `skill` | 只放行 `gaussdb-*` | 同左 |
| `read` / `glob` / `grep` / `list` | 放行（`/nas/kb` 在项目目录之外，`external_directory` 也放行） | 同左 |
| `bash` | 先全拒；放行 `python3` / `python` 执行技能目录 `gaussdb-*` 下的脚本，以及 `cat` `grep` `head` `tail` `ls` `wc` `sort` `uniq` `cut` `date` `echo` `pwd` `ps` `df` `du` `free` `find` `cd` `stat` `file` `tree` `realpath` `basename` `dirname` `which` `diff` `true`；拒掉重定向，但放行的命令可以带无害结尾（`2>/dev/null`、`2>&1`、`>/dev/null`）；最后再拒：两个以上 `>`（借安全结尾夹带写文件）、跳出技能目录（`/../../`）、`/proc/`、`environ`、`find` 的 `-exec` / `-delete` / `-fprint` 等、`env` / `printenv` / `set`（`export` / `declare` 也写了规则，但 opencode 不核对，见下） | 同左 |

opencode 的 bash 规则是「最后一条匹配的生效」，管道、`&&`、`$()` 里的每条命令分别核对，重定向（`>`、`<`、heredoc）算在命令里。
这一层是纵深防御：凭据本来就不在这个容器里。实测（`scripts/check-opencode-permissions.py`，真 opencode + 按脚本发工具调用的假模型）两点要知道：

- **`export`、`declare -p` 这类声明命令 opencode 不核对**（它只给语法树里的「命令」节点生成匹配项），能打印本进程的环境。
  所以环境里不放凭据：剩下的只有本人的对话口令、报告令牌这类本来就是他自己的东西。
- **无害的重定向结尾只给放行过的命令补**（2026-10-24 起）：模型几乎每条命令都爱加 `2>/dev/null` / `2>&1`，
  一律拒掉时会白撞一次（测试环境导入工单时连撞三次）。规则是通配符，不能写成「任何命令 + 安全结尾」
  （那等于放行 `curl … 2>/dev/null`），而是每条放行的命令各补几种结尾；一条命令里有两个以上 `>` 一律拒
  （`cat > x.py 2>/dev/null`）；读 `/proc`、环境变量带了安全结尾照样拒。

## 镜像里没有的东西

令牌、密钥、客户地址、`auth.json`、任何会话数据。`share` 在渲染的配置里固定为 `disabled`。

## 两个镜像的差异

| | runtime | kb-import |
|---|---|---|
| skills | 16 个诊断 skill + `gaussdb-kb`（查询） | `gaussdb-kb` + `gaussdb-kb-import` + `gaussdb-kb-init`（入库前标准化） |
| `/nas/kb` | 只读 | 读写 |
| `GRMP_API_HOST` / `GRMP_AUTH_TOKEN` | 必填 | 不需要（给个占位值即可，不接触数据库；`credproxy` 容器里 `GRMP_API_HOST` 置空，只代理模型） |
| `AGENT_ROLE`（工具权限） | `runtime`：不能编辑文件 | `kb-import`：可以编辑文件（写草稿） |
| 出站网络 | 中间件、模型服务、DNS | 模型服务、DNS |
| 谁能拿到 | 所有用户 | 知识库管理员 |
