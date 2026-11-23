# 接入手册：把 GaussDB 智能体接进贵方的 SSO 与 K8s

> 面向：贵方的平台/运维/安全团队。
> 目标：读完能独立完成部署与接入，并清楚哪些是我方交付、哪些需贵方提供、哪些尚未确定。

---

## 0. 先看这一页

### 0.1 这套东西是什么形态

**一人一容器。** 每个使用者登录后拥有一个属于自己的 Pod，里面跑着诊断智能体；用完回收，
下次登录重新拉起。用户的会话、历史、上传的文件全在 NAS 上，**Pod 是一次性的，数据不是**。

> 本手册讲的是默认的**按需模式**：我方网关按工号现建 Pod。部署时固定建好一批 Pod、登录时按槽位分配的
> **固定池化**（`RUNTIME_MODE=pool`，同样由我方网关调度）见《固定池化手册》。两种模式的 NAS 目录结构相同，可以来回切。

```
                                        ┌──────────────────────────┐
 浏览器 / CLI ──► 贵方 SSO/WAF ──────►  │ gaussdb-agent-gateway     │
                  (认人,把工号         │  · 认工号头凭什么可信      │
                   放进请求头)          │  · 按工号找/建 Pod         │
                                        │  · 反代 + 注入口令         │
                                        │  · 回收闲置 Pod            │
                                        │  · /dash 转大盘前端        │
                                        └────────────┬─────────────┘
                                                     │
                   ┌─────────────────────┬───────────┼──────────────────────┐
                   ▼                     ▼           ▼                      ▼
             frontend(共用)      runtime-<工号A>   runtime-<工号B>    kb-import-<工号C>
             四个大盘的静态页    17 个诊断技能     17 个诊断技能       知识库导入(管理员)
             不读任何数据        + 4097 报告端口   + 4097 报告端口
                                         │           │                      │
                                         └─────┬─────┴──────────────────────┘
                                               ▼
                         NAS(RWX)   users/<工号>/  admins/<工号>/  kb/(共享)
                         知识库索引就是 kb/index/store.sqlite 一个文件(SQLite + sqlite-vec),
                         kb-import 写,runtime 与池化槽位只读 —— 没有单独的向量库组件
```

### 0.2 五个镜像

| 镜像 | 形态 | 内容 |
|---|---|---|
| `gaussdb-agent-runtime` | **每个用户一个** Pod | 17 个诊断技能 + 知识库只读查询(只读打开 `kb/index/store.sqlite`);另开一个只读端口把本人的技能报告端给大盘 |
| `gaussdb-agent-kb-import` | **每个知识库管理员一个** Pod | 知识库导入与入库前标准化,共 3 个技能;`kb.py index` 在这里写知识库索引 |
| `gaussdb-agent-gateway` | **全体共用**,2 副本,常驻 | 认身份、建/收 Pod、反向代理 |
| `gaussdb-agent-frontend` | 全体共用,1 副本,常驻 | 四个大盘(健康检查 / Top SQL / WDR / 知识库)的**静态页面**。不读任何人的数据:页面在浏览器里经网关向用户自己的 Pod 取报告 |
| `gaussdb-agent-kbstd` | 部署时跑一次的 Job,装完退出 | **标准知识库**:GaussDB 官方文档集(G25132C,25.1.32 集中式,6023 条)。`kubectl apply` 后自动装进 NAS 的 `kb/docsets/`,同版本跳过,不碰贵方自己的知识;不要任何网络 |

### 0.3 我方交付 / 贵方提供 —— 一页看清

| 项 | 谁做 | 说明 |
|---|---|---|
| 五个镜像(含标准知识库) | **我方** | 发布在 GHCR(私有),由我方按人开通后贵方用自己的 GitHub 账号拉取;或走离线压缩包。均含 amd64 与 aarch64。见部署手册 §1.1 |
| K8s 清单、脚本、全部文档 | **我方** | **公开仓库**,不需凭据:github.com/sqlrush/gh_gs_k8s_agent |
| 按工号建/收 Pod、反代、回收 | **我方**(gateway) | 也可只用控制 API,接贵方已有调度器 |
| 身份认证(SSO 本身) | **贵方** | 我方只要一个稳定工号,见 §2 |
| **按人签发的 GRMP 令牌** | **贵方** | 见 §5.3,这是唯一必须按人准备的东西 |
| NAS(RWX 卷) | 贵方 | 见 §3.2 |
| K8s 集群与 RBAC 授权 | 贵方 | 见 §3 |
| 模型服务 + **embedding 端点** | 贵方 | 见 §6 |
| 中间件 GRMP 的按人授权 | 贵方 | 见 §9.1 —— **尚未确认** |

---

## 1. 整体设计的三条前提

理解这三条，后面的配置才不会配错。

### 1.1 工号决定一切

一个稳定的用户标识（下称**工号**）决定了：

```
工号 u1234
   ├─► Pod 名        runtime-u1234
   ├─► NAS 挂载      subPath=users/u1234     ← 他的会话、历史、上传文件
   ├─► GRMP 令牌     Secret grmp-token-u1234  ← 他在数据库里的权限
   ├─► 角色判定      在不在知识库管理员组里
   └─► 审计落款      报告上的「执行人」
```

**所以工号必须稳定、唯一、不可重用。**

- *不稳定*（今天 u1234 明天 zhangsan）→ 用户的历史会话找不到了；
- *重用*（老员工离职后工号给新人）→ 新人继承了老人的会话与数据库权限。

### 1.2 状态不在 Pod 里，所以不需要「还原」

用户的一切都在 NAS 的 `users/<工号>/` 下。新 Pod 挂上这个目录就恢复，
**网关不复制、不恢复任何数据**。「按用户名确定历史」= 用工号拼出挂载路径。

> **状态放在 Pod 本地盘**：Pod 启动时把状态从 NAS 载入本地，读写在本地，
> 每 60 秒回写，销毁前完整回写。这样绕开了 SQLite 直接跑在 NFS 上的性能瓶颈。
> 相应的代价见 §7.4。

### 1.3 隔离靠三层，缺一层就漏

| 层 | 机制 | 挡住什么 |
|---|---|---|
| Pod | 一人一 Pod，`.owner` 独占标记 | 别人的进程读到你的会话文件 |
| NAS | 主容器只挂 `subPath=users/<工号>` | 模型所在的容器看不到别人的目录（启动前的初始化步骤挂 `users/`，只建本人目录、不运行模型，结束即退出） |
| 网络 | NetworkPolicy | Pod 之间互访、Pod 出网 |
| **数据库** | **GRMP 中间件按人授权** | 看得到哪些库、哪些数据 —— **见 §9.1** |

---

## 2. 接入贵方的 SSO

### 2.1 我方需要的只有一样东西

> **在每个到达网关的请求里，带上一个可信的工号。**

协议是 OIDC、SAML、CAS、还是 4A，我方完全不关心 —— 那是贵方 SSO 与网关之间的事。
网关只读两个请求头：

| 请求头 | 必需 | 内容 | 配置项 |
|---|---|---|---|
| `X-Agent-User` | 是 | 工号 | `USER_HEADER` |
| `X-Agent-Roles` | 否 | 角色/组，逗号分隔 | `ROLES_HEADER` |

头名都可改。工号会被转小写并校验格式（见 §2.4）。

### 2.2 「可信」怎么保证 —— 这是整个系统的安全边界

**谁能把 `X-Agent-User` 头发进网关，谁就能进入任何人的环境。** 所以网关必须能判断
「这个头是贵方 SSO 放的，不是外面伪造的」。两道门，**至少配一道**：

| 方式 | 配置 | 上游要做什么 |
|---|---|---|
| **共享密钥**（推荐） | Secret `gateway-auth.TRUST_SECRET` | 每个请求带 `X-Agent-Trust: <密钥>` |
| **来源网段** | ConfigMap `gateway-config.TRUST_CIDRS` | 无（按源 IP 判定） |

两道都没配时**网关拒绝启动**。这是刻意的：不限制来源的网关是一个
「任填工号即可进入任何人环境」的接口，而且从外面看它工作得好好的。

> 共享密钥用常数时间比较，至少 16 字符。轮换时必须与上游同时改，
> 否则所有用户会在改完那一刻全部 403。

### 2.3 三种常见现状，各一条路

> SSO 对接好之前想先在现场用浏览器测试：见《现场测试-模拟SSO》，本机起一个模拟 SSO 即可，不改集群配置。

#### A. 贵方已有带 SSO 的 API 网关 / WAF（最常见）

在它上面加一条转发规则即可：

```nginx
location /gaussdb-agent/ {
    # 1) 这里已经过了贵方的 SSO,$remote_user / $http_x_userid 之类已可用
    proxy_set_header X-Agent-User   $remote_user;
    proxy_set_header X-Agent-Roles  $user_groups;
    proxy_set_header X-Agent-Trust  "<与网关约定的共享密钥>";

    proxy_pass http://gateway.gaussdb-agent.svc/;

    # 2) 下面四行是 SSE 必需的,漏了页面会一直转圈(见 §8.1)
    proxy_buffering       off;
    proxy_cache           off;
    proxy_http_version    1.1;
    proxy_read_timeout    1h;
}
```

#### B. 贵方只有 4A / 统一认证，没有可改的反向代理

在 K8s 里用 Ingress + 一个认证中间件（如 `oauth2-proxy`）对接 4A，
认完把工号写进头再转给网关。我方可协助配置，但该组件不在交付范围内。

#### C. 什么都没有（仅试点阶段适用）

在网关前放一个最小的静态映射（Basic 认证 → 工号），仅用于试点。
**不建议进入生产**：没有集中的账号生命周期管理，离职人员无法批量回收。

### 2.4 工号的格式约束

工号会被拼进 K8s 资源名和 NAS 路径，所以有硬约束：

- 只允许 `a-z`、`0-9`、`-`；首尾必须是字母或数字（RFC 1123 标签的子集）
- 最长 **54** 字符（`runtime-` 前缀占 8，K8s 标签上限 63）
- 大写会被转成小写 —— 贵方须保证大小写不敏感下仍然唯一
- 不合法的工号直接拒绝，**错误信息不回显原值**（避免注入落点）

### 2.5 知识库管理员怎么进导入容器

管理员（角色头带 `KB_ADMIN_ROLES` 之一，或工号登记在 ConfigMap `agent-roles` 里）登录时，
网关除了诊断容器 `runtime-<工号>` 还会建导入容器 `kb-import-<工号>`。对话默认进诊断容器，进导入容器有两种方式：

| 方式 | 怎么做 | 适合 |
|---|---|---|
| 浏览器切换 | 访问 `<网关地址>/kb-import/`，网关记一个 cookie 并跳到对话页；`<网关地址>/runtime/` 切回诊断容器 | 同一个入口，管理员自己切 |
| 单独的入口 | 贵方 SSO / WAF 为管理员另配一个入口，转发时加请求头 `X-Agent-Target: kb-import` | 想给管理员一个固定书签、或 `opencode attach` 命令行 |

- **每个请求都在网关重新校验管理员身份**：不是管理员时 `/kb-import/` 回 403，带着 cookie 或请求头也只会进诊断容器。
- 大盘（`/dash`）与报告（`/reports`）只在诊断容器里，不受切换影响。
- cookie 按站点生效：切到导入容器后，这个浏览器里新开的对话标签页也在导入容器里，要回诊断容器访问一次 `/runtime/`。

---

## 3. 接入贵方的 K8s

### 3.1 集群前提

| 项 | 要求 | 不满足会怎样 |
|---|---|---|
| K8s 版本 | ≥ 1.24 | 更低版本未验证 |
| CNI | **必须支持 NetworkPolicy**（Calico / Cilium 等） | 策略不生效，隔离只剩 Pod 与 subPath |
| StorageClass | 一个 RWX 卷（NFS v4.1） | 见 §3.2 |
| 架构 | x86_64 或 aarch64（鲲鹏） | 镜像两种都提供。**鲲鹏未实测**，见 §9.4 |
| 命名空间 | `gaussdb-agent`（可改，改时全套清单一起改） | |

### 3.2 NAS

```
NAS 根/
├── users/<工号>/     普通用户:会话、历史、workspace、上传文件
├── admins/<工号>/    知识库管理员:同上
└── kb/               知识库,**全体共享**(runtime 只读,kb-import 读写)
```

- PV 的 `mountOptions`：`[nfsvers=4.1, hard, local_lock=all]`
- 导出目录对 **uid 1000** 可写（容器内以 uid 1000 运行）
- `users/`、`admins/`、`kb/` 三个目录由贵方预先建好（uid 1000）
- 容量：按人 1–5 GB 估算；知识库另算

> **压测要求**：NFS 在 5 天连续负载下的稳定性与 `mountOptions` 生效验证，
> 需在贵方环境完成并签字（我方无法代做）。

### 3.3 网关需要的 RBAC

清单 `k8s/base/gateway.yaml` 已包含，权限刻意收窄：

```yaml
apiGroups: ["apps"]  resources: ["deployments"]  verbs: [get,list,create,patch,update,delete]
apiGroups: [""]      resources: ["services"]     verbs: [get,create,patch,update,delete]
apiGroups: [""]      resources: ["secrets"]      verbs: [get,create,patch,update,delete]   # 注意没有 list
apiGroups: [""]      resources: ["configmaps"]   verbs: [get]
apiGroups: [""]      resources: ["pods"]         verbs: [get,list]   # 只读:用户容器起不来时读原因
apiGroups: [""]      resources: ["events"]       verbs: [list]       # 同上
```

**两条刻意的缺口**，请贵方安全审查时确认：

- **secrets 没有 `list`**：给了 list 就等于一次请求拿到全体用户的口令。
  `get` 是为了「已有口令就沿用，不轮换正在用的口令」。
- **没有 `pods/exec`、`pods/attach`、`pods/portforward`、`pods/log`**：网关没有进用户容器、读用户输出的理由。
  它对 Pod 只有只读的 `get/list`（加上事件的 `list`），用来在用户容器起不来时说清是哪一类原因
  （拉不到镜像、配置缺失、个人目录初始化失败、NAS 挂不上、调度不上），而不是让用户等满超时只看到「正在启动」。

全部是 **Role**（命名空间内），没有任何 ClusterRole。

### 3.4 一次性部署步骤

```bash
# 1) 载入镜像(离线包)
for f in gaussdb-agent-*-<arch>.tar.gz; do docker load -i "$f"; done
# 推到贵方内网仓库,并把清单里的 image 字段改成仓库地址

# 2) 基础资源
kubectl apply -k k8s/base

# 3) Secret(样例见 k8s/base/*.example.yaml,**不要**把真实值提交进版本库)
kubectl -n gaussdb-agent create secret generic model-api \
    --from-literal=MODEL_API_KEY='<模型服务密钥>'
kubectl -n gaussdb-agent create secret generic gateway-auth \
    --from-literal=TRUST_SECRET="$(openssl rand -hex 24)" \
    --from-literal=ADMIN_TOKEN="$(openssl rand -hex 24)"
kubectl -n gaussdb-agent create secret generic grmp-sm2 \
    --from-file=GRMP_SM2_PRIVATE_KEY=<SM2 私钥文件>      # 中间件开了签名校验才需要

# 4) 两个 ConfigMap(见 §4)
kubectl -n gaussdb-agent edit configmap agent-config
kubectl -n gaussdb-agent edit configmap gateway-config

# 5) 知识库:按需编辑 NAS 上的 kb/kb.yaml(embedding 端点,见 §6.3)。没有向量库要初始化 ——
#    索引 kb/index/store.sqlite 由管理员导入时 kb.py index 建出来

# 6) NetworkPolicy:把占位网段改成实际地址
kubectl -n gaussdb-agent edit networkpolicy runtime-policy kb-import-policy gateway-policy
```

### 3.5 如果贵方已有容器管理框架

网关的代理那一半可以完全不用，只调控制 API：

```
POST   /admin/pods/{工号}     建(幂等)。body 可带 {"kb_admin": true}
GET    /admin/pods/{工号}     查状态 {exists, ready, last_activity}
DELETE /admin/pods/{工号}     回收(NAS 数据保留)
GET    /admin/pods            列出全部
```

鉴权：请求头 `X-Admin-Token: <ADMIN_TOKEN>`。
**`ADMIN_TOKEN` 留空时整组接口返回 404**（连「需要鉴权」都不回）。

此时贵方自己负责：反向代理、注入 Basic 口令（口令在 Secret `pod-auth-<工号>` 里）、
以及判定何时回收。**注意 §8.1 的 SSE 要求同样适用于贵方的代理。**

---

## 4. 配置项全表

### 4.1 `gateway-config`（网关）

| 键 | 默认 | 说明 |
|---|---|---|
| `USER_IMAGE_TAG` | **无默认** | 给用户 Pod 用哪个镜像版本。**这是升级开关**：改它 + 重启网关 = 之后登录的人用新版，已在用的人不受影响。不给默认值是为了避免「升级」悄悄发生 |
| `PULL_POLICY` | `IfNotPresent` | |
| `USER_HEADER` | `X-Agent-User` | 工号在哪个头里 |
| `ROLES_HEADER` | `X-Agent-Roles` | 角色在哪个头里 |
| `KB_ADMIN_ROLES` | `kb-admin` | 哪些角色值算知识库管理员 |
| `REAP_MODE` | `scale` | `scale`=缩到 0 副本；`delete`=删闲置容器的 Deployment 与 Service。见 §7.2 |
| `IDLE_SECONDS` | `1800` | 闲置多久回收。**下限 300 秒**，低于此值拒绝启动 |
| `TRUST_CIDRS` | 空 | 允许携带工号头的来源网段 |

Secret `gateway-auth`：`TRUST_SECRET`、`ADMIN_TOKEN`。

### 4.2 `agent-config`（用户 Pod）

| 键 | 说明 |
|---|---|
| `GRMP_API_HOST` / `GRMP_API_PORT` | 中间件地址 |
| `GRMP_PATH_PREFIX` | 中间件接口路径前缀，**必填**，中间件团队提供（以 `/` 开头、只写路径、末尾不带 `/`，形如 `/xxx/grmp/diagnostic/agent`）；没填时登录数据库直接报「GRMP_PATH_PREFIX 没配」 |
| `GRMP_APPKEY` | 调用方应用名。空 = 不带签名头 |
| `GRMP_SIGN_MODE` | `full`（三个头）/ `headers-only`（只 Appkey + Timestamp）。见 §5.2 |
| `GRMP_SIGN_TIMESTAMP` | `ms` / `s` |
| `GRMP_USER_ID_HEADER` / `GRMP_USER_ID_PARAM` | 把工号发给中间件时用的字段名。见 §5.4 |
| `GRMP_INSTANCE_ALIASES` | 实例别名：`别名=实例IP/库名  # 说明`，换行或分号分隔。空 = 不登记（参数手册「实例别名」） |
| `MODEL_BASE_URL` / `MODEL_ID` | 模型服务 |

---

## 5. 与 GRMP 中间件对接

### 5.1 请求形态

```
POST <中间件>/...
头:  auth       本人令牌(按人签发,见 §5.3)
     Appkey     调用方应用名(整个智能体一个)
     Timestamp  时间戳
     Signature  SM2 签名「URL 上下文根 + 时间戳」
报文: {"dataIp": "<实例IP>", "id": "<脚本ID>", "param": [...]}
```

### 5.2 过渡期：先不带 Signature

若 SM2 密钥尚未签发，可先只加 Appkey 与 Timestamp，**不加 Signature**：

```yaml
GRMP_APPKEY: <中间件给的 Appkey>
GRMP_SIGN_MODE: headers-only     # 此档不读私钥
GRMP_SIGN_TIMESTAMP: s           # 秒级,形如 1758431954
```

密钥到位后把 `GRMP_SIGN_MODE` 改回 `full` 并创建 Secret `grmp-sm2` 即可，无需换镜像。

> **我方刻意没做「拿不到私钥就自动降级」**：那样一来，生产上 Secret 挂载失败会让
> 「全签」悄悄变成「不签名」，安全等级降了而没人知道。降级必须是配置里写明的决定。

### 5.3 按人签发的令牌 —— 贵方必须准备

每个使用者的 `auth` 令牌存在 Secret `grmp-token-<工号>` 里，key 为 `GRMP_AUTH_TOKEN`。

**网关不创建它**，因为令牌由贵方中间件按人签发，网关无从得知。
开通一个用户 = 创建这个 Secret：

```bash
kubectl -n gaussdb-agent create secret generic grmp-token-u1234 \
    --from-literal=GRMP_AUTH_TOKEN='<该用户的令牌>'
```

缺它时用户登录会得到 **503「您的环境尚未开通」**，网关日志里写明缺的是哪个 Secret。

### 5.4 把工号发给中间件

中间件需要显式收取调用人工号时，字段名与位置均可配置（请求头或报文顶层，按中间件口径二选一）：

```yaml
GRMP_USER_ID_HEADER: "X-User-Id"   # 放请求头
GRMP_USER_ID_PARAM:  "userId"      # 或放报文顶层
```

两个都留空 = 不发工号。**填了字段名而取不到工号时，建连接就报错，绝不发空工号** ——
空工号会让中间件看到「有调用但没有身份」，而我方仍然退出码 0。

---

## 6. 知识库与向量检索

### 6.1 三层存储

| 层 | 技术 | 缺席的后果 |
|---|---|---|
| 文件 | Markdown + YAML 三元组 + 词法检索 | 必需 |
| 向量 + 词法索引 | `kb/index/store.sqlite`（SQLite + sqlite-vec，**一个文件，不用部署**） | 还没建索引时退回文件模式（词法照常）；**缺向量才真会漏**：换了说法的查询召回不到 |
| 图 | Neo4j | **无损失**：自动退回读 `graph/*.yaml`，答案一样 |

**真相永远是 `cases/` 下的文件**，索引文件与图库都是可随时重建的派生索引（`kb.py index --rebuild`）。

### 6.2 向量库：知识库目录里的一个文件

2026-10-23 起，向量 + 词法索引是 NAS 上知识库目录里的 `kb/index/store.sqlite`（SQLite + sqlite-vec 扩展，
已装进 runtime 与 kb-import 镜像），原来的 openGauss 向量库组件下线：**不需要数据库实例、库用户、口令、Secret 与出网规则**。

- **只有 kb-import 写**：`kb.py index` 持知识库写锁，在同目录的临时副本上写、写完原子替换；
- **runtime 与池化槽位只读**：以只读方式打开（`mode=ro&immutable=1`），对知识库的只读挂载照样能用，任何时刻读到的都是完整的一份；
- 词法是我方代码在索引里的倒排上算 BM25（与文件模式同一套算式）；向量用 sqlite-vec 的 `vec_distance_cosine` **精确**检索，几万块以内够用；
- 还没建索引或索引打不开时，状态行写「模式：文件（原因）」，检索直接在 `kb/` 文件上做，只少向量召回。

读写分离靠挂载：runtime 对 `kb/` 是只读挂载，没有任何口令可泄露。从带向量库的旧版本升级见部署手册 §10.6。

### 6.3 embedding 端点 —— 贵方必须提供

**这是硬依赖。** 索引文件不用部署，但没有 embedding 端点，向量层一样用不起来。

请确认三件事：

1. 贵方模型服务**是否提供** OpenAI 兼容的 `/v1/embeddings`？
   （embedding 模型与对话模型是两种模型，vLLM 装一个对话模型时 `/v1/embeddings` 会 404）
2. 用哪个 embedding 模型？
3. 输出多少维？

若没有，需要另外准备一个 embedding 服务 —— 属于环境准备，不是改配置，**前置时间长，建议尽早确认**。

配置在 NAS 上的 `kb/kb.yaml`（全体共用一份）：

```yaml
embeddings: {source: url, base_url: http://<贵方端点>/v1, model: bge-m3, dims: 1024}
```

不需要 `store.pg` 段（旧版本留下的会被忽略，`kb.py index` / `kb.py health` 提示可以删）。

> **换 embedding 模型必须重建索引。** 同维度换模型而不重建，索引里会混着两套向量空间，
> 相似度失去意义而覆盖率仍显示 100%。我方已加闸：换了模型不加 `--rebuild` 直接拒绝。

---

## 7. 生命周期

### 7.1 一次登录的完整时序

```
1. 用户经贵方 SSO 登录
2. 贵方网关把请求转给我方网关,带上工号与信任凭据
3. 我方网关:验信任 → 查 Pod
     不存在 → 检查 grmp-token-<工号> 在不在(不在 → 503)
            → 渲染模板、apply Deployment + Service
            → 等就绪(默认上限 120 秒,冷启动实测约 6 秒)
     已存在且就绪 → 直接用(**不做 apply**,见下方注)
4. 从 Secret 取该用户的 Basic 口令,注入 Authorization 头
5. 反向代理,转发字节时更新 Deployment 上的 last-activity 注解
```

> 注：热路径上不做 apply，有两个原因：① server-side apply 会按模板重写对象，
> 把活动注解抹掉，回收器因此看不到活动，最终会收掉**正在用的** Pod；
> ② 每个请求少一次 K8s 写。

### 7.2 两种回收模式

| 模式 | 动作 | 适用 |
|---|---|---|
| `scale`（默认） | 缩到 0 副本 | 省掉镜像拉取与冷启动。实测重连 7.6 秒拉起，会话原样还在 |
| `delete` | 删闲置的那个容器的 Deployment 与 Service | 彻底释放。口令与 GRMP 令牌 Secret 保留，下次登录按模板重建，NAS 数据在，**不需要任何还原步骤** |

**两种模式都不动 PVC 与 NAS 目录。**

### 7.3 闲置判定 —— 请特别注意

**不能按「N 分钟无新请求」判闲置。** opencode 的事件流（SSE）每 10 秒发一次心跳，
用户开着页面发呆时**有字节流动但没有新请求**。按请求计数判定会把正在用的人的 Pod 收掉。

我方网关按**转发字节**更新活动时间。若贵方自建调度器，请采用同样口径。

### 7.4 状态放在本地盘的代价

| | 优雅关闭 | 非优雅关闭 |
|---|---|---|
| 触发 | SIGTERM（删 Pod、缩容、滚动更新） | OOMKilled、节点故障、`--force --grace-period=0`、emptyDir 撑爆被驱逐 |
| 数据 | **零丢失**（完整回写后才退出） | **丢最后一个同步周期**（默认 60 秒） |

非优雅关闭没有执行收尾代码的机会，这是架构性的，补不了。我方能做的是**让它可见**：

- 启动时打 `dirty` 标记，走完收尾才清除；
- 下次启动发现标记还在 → 先给 NAS 留一份 `.unclean-<时间戳>/` 快照，并在日志中明确告警。

否则用户只会觉得「我明明问过那个问题」，而没人会想到是 Pod 被 OOMKill 了。

---

## 8. 贵方反向代理的要求

### 8.1 必须关闭缓冲（唯一的硬要求）

```nginx
proxy_buffering off;
proxy_cache off;
proxy_http_version 1.1;
proxy_read_timeout 1h;
gzip off;
```

后端已自带 `x-accel-buffering: no`（nginx 系认这个头），但 F5、Apache、部分 WAF 不认，需显式配置。
**没关缓冲的表现是页面一直转圈、消息迟迟不出来 —— 不是「断了」，是「卡住了」，最难排查。**

### 8.2 请确认的两条硬限制

1. **单连接最长持续时间**。部分 WAF 设有「单个连接最长 N 分钟」。SSE 是永不结束的连接，
   会被周期性斩断。前端会自动重连（带 `Last-Event-ID`），不至于不可用，但重连瞬间的
   状态残留会表现为「已中断」。
2. **闲置回收的判定口径**。若贵方也有闲置回收，请按 §7.3 采用字节活动口径。

### 8.3 用户可能遇到的「已中断」

opencode 的 Web 界面里，**在空输入框上按回车 = 中断当前回合**。
中文输入法回车确认候选词后再回车发送，第二次回车容易落在空框上。

> 建议写进用户须知：*看到「已中断」，刷新页面（F5），把上一条重新发一遍即可。*

---

## 9. 尚未确定 / 需贵方确认的事项

> 以下每一条都会影响交付效果，建议在正式投产前逐条答复。

### 9.1 中间件的按人授权（优先级最高）

我方隔离设计的第四层（数据库可见范围）**完全依赖中间件**。请确认：

1. 诊断 API 的 `auth` 令牌，能否**按调用人分别签发**？还是整个接入方一个？
2. 若能按人签发，不同令牌是否**映射到不同的数据库执行账号**？还是最终都用同一个账号执行 SQL？
3. 数据库侧的审计日志能否区分出是哪个工号发起的操作？

**第 2 问是关键。** 若最终都用同一个执行账号，则一人一 Pod 的隔离只覆盖会话与文件，
**数据库可见范围与审计留痕对全体用户相同**。

**第 3 问对审计合规是硬性的** —— 若追溯不到人，需贵方评估是否可接受。

> 我方现状：已实现「每人一个令牌 Secret 并注入本人 Pod」这一承载机制，
> 但**从未在按人签发的真实令牌上验证过端到端行为**（测试环境只有一个令牌）。

### 9.2 一个 dataIp 是否只对应一个实例

接口报文的定位字段只有 `dataIp`，没有端口或实例 ID。若存在同 IP 多实例（不同端口），
中间件如何区分？报文是否有扩展位？

同实例多库、而白名单按库区分时，仅凭 `dataIp` 拿到的命令清单是否正确？

### 9.3 签名口径

`Signature` 是**签名**还是**加密**？（贵方原文用词为「加密」，但 SM2 用私钥做的是签名）
以及 userId、时间戳单位、编码、格式、原文拼法各是什么口径 —— 旋钮全部可配，对不上不用改代码。

### 9.4 鲲鹏硬件

镜像提供 aarch64 版本，但**从未在真实鲲鹏硬件上运行过**。已知风险：
鲲鹏常见的 64K 页大小配置对 Bun 运行时的兼容性未验证。建议在正式部署前先做一次冒烟。

### 9.5 现场存量版本的签名能力

现场运行的非容器版本 v12.9.x **不具备签名能力**。中间件一旦开启签名校验，现场会全停。
请告知**启用日期**，以便安排升级窗口。

---

## 10. 附：排查入口

| 现象 | 先看哪里 |
|---|---|
| 用户进不去，503 | 网关日志 —— 多半是缺 `grmp-token-<工号>` |
| 用户进不去，503「启动失败（某一类原因）」或 504 | 先看网关日志：`kubectl -n gaussdb-agent logs -l app=agent-gateway --prefix \| grep <工号>`，网关已把 Pod 状态与告警事件写在那一行里；再细看 `kubectl describe pod -l user=<工号>` |
| 页面一直转圈 | 贵方代理的缓冲设置（§8.1） |
| 页面「已中断」 | §8.3；网关日志搜 `cancel` |
| Pod 被意外回收 | 闲置口径（§7.3）；`kubectl get deploy runtime-<工号> -o jsonpath='{.metadata.annotations}'` |
| 丢了对话 | Pod 日志搜「非优雅」；NAS 上找 `.unclean-*` 快照（§7.4） |
| 知识库检索不准 | `kb.py health` 看模式与「向量:」：还没建索引（文件模式）或 kb.yaml 没配 embeddings 时只有关键词召回（§6.3） |

网关自身：`kubectl -n gaussdb-agent logs -l app=agent-gateway`
用户 Pod：`kubectl -n gaussdb-agent logs deploy/runtime-<工号>`
（启动时会打印独占标记、状态加载、数据库自检三行，出问题先看这三行）
