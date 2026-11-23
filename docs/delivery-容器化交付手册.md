# 容器化交付手册

版本对应：本仓库标签 **agent-v0.7**，技能代码与 gh_skill **skills-v12.15** 为同一份（逐版对应见 `agent/UPSTREAM`），opencode **1.18.27**。设计与验证记录见 `docs/specs/`、`docs/plans/`。

## 0. 两种交付形态，客户二选一（或并存）

| | 容器版（本仓库） | 非容器版（gh_skill） |
|---|---|---|
| 交付物 | 两个镜像 + k8s 清单 + 本手册 | 技能压缩包 + `install-opencode.sh` |
| 运行方式 | 每人一个 Pod，平台按工号供给 / 回收 | 客户自己装 opencode，技能装进 `~/.config/opencode/skills` |
| 技能集 | **按角色物理拆开**：runtime 17 个（诊断 + 知识库查询）；kb-import 3 个（查询 + 导入 + 入库前标准化） | **全都有**：18 个，`gaussdb-kb` 是查询+导入合一 |
| 知识库隔离 | 普通用户 Pod 对 `kb/` **只读**，镜像里根本没有导入命令 | 同一套技能里就有导入命令，**靠流程约束，不是物理隔离** |
| 状态 | NAS（`users/<工号>/`），Pod 可随时重建 | 客户机器本地 `$GSDB_HOME` |
| 升级 | 换镜像标签，滚动重建 | 重跑安装脚本 |
| 适用 | 多用户、要隔离、要集中管控 | 单机 / 少数人、不想上 k8s |

**功能完全一致**（同一份技能代码）。差别只在**打包方式**与**隔离强度**——容器版的「普通用户拿不到导入能力」是镜像层面做掉的，非容器版做不到这一点，安全评审时要讲清楚。

## 1. 交付物

| 文件 | 内容 |
|---|---|
| `gaussdb-agent-v0.4.1-oc1.18.27-amd64.tar.gz` / `-arm64.tar.gz`（+ `.sha256`，各约 150 MB） | 两个后端镜像：`gaussdb-agent-runtime`（每用户一个 Pod）、`gaussdb-agent-kb-import`（知识库管理员）。x86_64 与鲲鹏各一包 |
| `gaussdb-agent-v0.4.1-oc1.18.27-k8s.tar.gz`（+ `.sha256`） | `k8s/base` 一次性清单、`k8s/templates` 按工号渲染的模板、`docs/env-contract.md` 环境契约、`docs/k8s-deploy.md` 部署手册、本手册、设计文档 |
| `MANIFEST.txt` | 镜像 id、大小、仓库提交号、导入命令 |

由 `scripts/package-images.sh agent-v0.4.1-oc1.18.27` 生成到 `dist/gaussdb-agent-v0.4.1-oc1.18.27/`；发布前的验证是删掉本地镜像、从包 `docker load` 回来、再跑 `scripts/smoke-image.sh`（14 项，只读根文件系统）。镜像里**没有**：令牌、密钥、客户地址、`auth.json`、任何会话数据。

## 2. 平台必做

**一次性**（`docs/k8s-deploy.md` §1–2）

1. 镜像导入内网仓库，两种架构。
2. NAS：RWX 卷，NFSv4.1；PV `mountOptions: [nfsvers=4.1, hard, local_lock=all]`；导出目录对 uid 1000 可写；预建 `users/<工号>/`、`admins/<工号>/`、`kb/`。
3. `kubectl apply -k k8s/base`（或 `k8s/overlays/test` / `prod`：同一镜像，只换 ConfigMap）；创建 Secret `model-api`；填 `agent-config`（中间件、模型服务地址与模型 id）；`agent-roles`（没有 AD 组时的知识库管理员名单）；把 NetworkPolicy 的占位网段改成实际地址段。CNI 必须支持 NetworkPolicy。
4. 中间件 GRMP 按人签发令牌、按人授权（库这一层的隔离靠它）。
5. **中间件签名校验**（2026-09-18 加固）：向中间件方登记本智能体的 Appkey（一个即可）并取得该 Appkey 的 SM2 密钥对；私钥 `kubectl create secret generic grmp-sm2 --from-file=GRMP_SM2_PRIVATE_KEY=<文件>`，`agent-config` 填 `GRMP_APPKEY` 与签名旋钮（`GRMP_SIGN_*`，见 `docs/env-contract.md`）。旋钮的值必须与中间件方逐项确认（第 7 节表）。

**按工号**（网关，`docs/k8s-deploy.md` §3–4）

1. SSO 鉴权得到工号；判定角色（AD 组或 `agent-roles`）。
2. 建 Secret `grmp-token-<工号>`（本人令牌）与 `pod-auth-<工号>`（随机口令）。
3. 渲染 `k8s/templates/runtime.yaml`（管理员再加 `kb-import.yaml`），apply，等 `availableReplicas == 1`。
4. 代理到 Service `runtime-<工号>:4096`，带 `Authorization: Basic base64(opencode:<口令>)`；浏览器落地页 `/L25hcy9tZS93b3Jrc3BhY2U`。
5. 回收：对话结束删 Deployment/Service/Secret，或闲置超时 `scale --replicas=0`。同一工号同一时刻只能有一个 runtime Pod。
6. 文件上传：网关直接写 NAS——普通用户 `users/<工号>/workspace/uploads/`，知识库管理员另有 `kb/inbox/uploads/`；uid 1000、文件名白名单、大小上限、回给用户 Pod 内路径（`docs/k8s-deploy.md` §3）。

`scripts/k8s/provision.py` 是 2–3–回收的参考实现，可直接改造进网关。

## 3. SQLite 数据库文件位于 NAS 的说明（需客户确认）

运行环境把 opencode 的会话数据库（SQLite，WAL 模式）放在 NAS 挂载路径上运行。SQLite 官方文档指出 WAL 模式不支持网络文件系统，主要风险是多进程同时访问导致的数据损坏。本部署通过以下措施将该风险控制在单点故障范围内：NFSv4.1 挂载、`local_lock=all`、平台保证同一用户同一时刻仅一个 Pod、Pod 启动时的独占标记（`.owner`）、优雅停机与 WAL 合并、启动完整性自检、退出时备份（保留 3 份）与 NAS 每日快照。

上线前在贵方 NAS 环境完成 5 个工作日压测（第 4 节），结果见附件。若压测未通过，改用「本地运行 + 持续复制到 NAS」方案（Litestream），这是唯一退路。

残余风险：Pod 被强制终止且平台未能保证独占时，可能出现数据库损坏，影响范围为该用户的对话历史，可从最近一次备份恢复（启动自检自动做，日志写明）。

贵方确认知悉上述风险并接受本部署方式。

| 确认人 | 部门 | 日期 | 签字 |
|---|---|---|---|
| | | | |

## 4. 上线前压测方案

- 环境：客户 NAS，20 个 runtime Pod 跨多节点同时在线，每个 Pod 用脚本驱动连续对话（含长输出，模型流式输出期间 opencode 高频写消息片段）。
- 时长：连续 5 个工作日。
- 采集：opencode 日志里 `SQLITE_BUSY` / `database is locked` 次数；每轮对话响应延迟 p50 / p95（对照组：同样负载跑在 emptyDir 本地盘）；每日 `PRAGMA integrity_check`；NAS 侧 IOPS 与延迟。
- 通过标准：零损坏；`locked` 为零或仅出现在 NAS 故障期间；p95 与对照组差距可接受（建议 < 30%）。
- 不通过：改 Litestream 方案，镜像侧只增加一个边车与 init 容器，NAS 目录布局不变。

## 5. 部署后冒烟（平台自查）

| 项 | 命令 / 看什么 | 期望 |
|---|---|---|
| Pod 就绪 | `kubectl -n gaussdb-agent get deploy runtime-<工号>` | 1/1；日志前三行「独占标记」「数据库自检」「日志清理」 |
| 界面与认证 | 浏览器打开落地页 | 不带口令 401；带口令看到「新建会话」页，模型选择器显示配置的模型 |
| 登录与诊断 | 在界面里输入「登录 <实例IP> 的 <库名>，评估死元组」 | 模型调 gaussdb-login / gaussdb-vacuum，回答带评估结论 |
| 知识库只读 | 输入「帮我导入工单」 | 回答「本环境不含知识库导入功能，请联系知识库管理员」 |
| 重建不丢 | 删 Pod，等 Ready，刷新界面 | 会话还在；NAS `users/<工号>/backup/` 多一份 |
| 管理员 | 管理员登录后有 kb-import Pod | `kb.py health` 不报只读；导入后 runtime 的 `kb.py search` 能命中 |
| 入库前标准化 | 在 kb-import Pod 的界面里说「把收件目录里这几份材料标准化」（word / pdf / 照片混放） | 模型调 gaussdb-kb-init：scan 点名照片要看图、不支持的格式留在清单；写完草稿 render 出 `init/<批次>/std/*.md`（六要素：时间、系统、现场、判断、处置、复发标志）；runtime Pod 里没有这个 skill |

本仓库自带的自动化版本：`scripts/k8s/verify-cluster.sh`（13 项）、`scripts/k8s/e2e-web.sh`（7 项，走 Web 界面同一套 API）。

## 6. 回滚

- 镜像：`provision.py --image-tag <上一版>` 重新 apply（Deployment 滚动，Recreate 策略保证不并存）。
- 数据：`opencode.db` 随镜像升级自动迁移且不可回退——升级前对 NAS 做快照；回退镜像时同时回退 `users/` 目录。
- 技能：`agent/` 与 gh_skill 的版本对应见 `agent/UPSTREAM`；技能层的热修走 gh_skill `release/v12.9`（非 Pod 部署）与本仓库补丁版本（Pod 部署）。

## 7. 尚未覆盖

| 项 | 状态 |
|---|---|
| 网关（鉴权、代理、闲置回收） | 平台做；本仓库给参考实现与契约 |
| Web 界面 | 第一版用 opencode 自带界面（已验证）；deepseek-harness 前端改造看客户要求 |
| NFS 压测与 `mountOptions` 生效 | 客户环境（第 4 节） |
| NetworkPolicy 在客户 CNI 上生效 | 客户环境；本仓库在 OrbStack 上验过策略生效 |
| 向量库 / 图谱库 Pod | 向量库不需要 Pod：2026-10-23 起向量 + 词法索引是 NAS 上的 `kb/index/store.sqlite`（SQLite + sqlite-vec，kb-import 写、runtime 只读）；图谱库未做，路径走 `graph/*.yaml`，与带图库时首选结果一致 |
| 中间件签名口径 | 代码与本地对照（mock）已就绪、全部可配；以下项需中间件方书面确认，任一项不符表现为全部请求被拒：① Signature 是 SM2 签名（SM3 摘要）还是「公钥加密」；② userId（国标默认 `1234567812345678`，OpenSSL 默认空串）；③ 原文拼法（`{path}+{timestamp}` 里的 `+` 是否字面、path 是否含完整路径 / 查询串）；④ 时间戳单位与允许偏差；⑤ 签名编码（hex / base64）与格式（raw / DER）；⑥ 私钥文件格式；⑦ 生效日期与兼容期；⑧ 校验失败的响应形态。建议中间件方提供一条可通过的样例请求或其校验代码 |
