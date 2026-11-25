# 快速搭建（鲲鹏 + openFuyao 沙箱，测试环境）

> 适用版本：`agent-v1.5.2`。本文是《Fuyao沙箱部署手册》的**简易版**：只走一条最短的路，按顺序复制命令即可，每条命令下面写着**应该看到的结果**。
> 容量规划、升级、扩容、卸载与完整的排障说明，仍以《Fuyao沙箱部署手册》为准。
>
> **只用于测试环境。** Fuyao 沙箱模式是预览功能：只接测试数据库、测试账号、测试环境的中间件，不接生产，不填生产令牌。
>
> 本文的命令取自《Fuyao沙箱部署手册》，该手册的实测环境：鲲鹏 920（ARM64）单节点 Kubernetes + openFuyao（OpenSandbox、Flux、E2B），网络插件 Flannel，2026-11。
> 资料齐全时，全程约 1 到 1.5 小时。

---

## 一、先看懂要装什么

```
浏览器 ──► 网关 ──► 沙箱（每人一台微型虚拟机，智能体在里面运行）
                      │
                      ├──► 状态服务 ──► NAS（保存会话、报告、知识库）
                      │
                      └──► 凭据代理 ──► 中间件（查数据库）
                                   └──► 模型服务（大模型）
```

| 名词 | 一句话说明 |
|---|---|
| 沙箱 | 平台为每位用户开的一台微型虚拟机，智能体在里面运行。诊断用一台；知识库管理员另有一台，用来导入知识 |
| 模板 | 沙箱的「系统盘」。我方提供两个：诊断模板、导入模板，要先注册到平台上 |
| 沙箱组 | 平台上预留的一块资源，决定最多能同时开几台沙箱。由平台管理员建 |
| 网关 | 我方的入口：认人、给人分配沙箱、转发请求 |
| 状态服务 | 把沙箱里的数据定时存到 NAS，沙箱没了数据还在 |
| 凭据代理 | 保管密钥。智能体访问中间件、模型服务都经过它，密钥不进沙箱 |
| NAS | 共享存储（NFS） |
| 中间件 | 访问数据库的统一入口。智能体只能通过它执行已经登记的诊断脚本 |
| Secret / ConfigMap | Kubernetes 里存密钥 / 存配置的对象 |

**会用到三种机器**（单机环境里它们是同一台）：

| 叫法 | 是哪台 | 怎么确认 |
|---|---|---|
| 部署机 | 能执行 `kubectl` 的机器，一般是集群主节点 | `kubectl get nodes` 能列出节点 |
| 集群节点 | Kubernetes 的每一个节点 | `kubectl get nodes` 列出的每一行 |
| 模板构建主机 | 平台的模板构建服务所在的那台主机 | `kubectl -n e2b get pods -o wide \| grep -i template`，看 `NODE` 一列 |

---

## 二、一共 8 步

| 步骤 | 做什么 | 在哪台机器 | 谁来做 | 大约多久 |
|---|---|---|---|---|
| 0 | 备齐资料 | — | 你，向各团队要 | 看各团队 |
| 1 | 下载安装包并校验 | 能上外网的电脑 → 拷进内网 | 你 | 20 分钟 |
| 2 | 导入 5 个镜像 | 每个集群节点 | 你 | 5 分钟 |
| 3 | 注册 2 个沙箱模板 | 模板构建主机 | 你（root） | 5 到 15 分钟 |
| 4 | 建 2 个沙箱组 | 部署机 | 平台管理员 | 5 分钟 |
| 5 | 准备 NAS | NFS 服务器或节点 | 存储管理员或你 | 5 分钟 |
| 6 | 填参数、建密钥、部署 | 部署机 | 你 | 15 分钟 |
| 7 | 开通用户并验证 | 部署机 | 你 | 10 分钟 |

---

## 第 0 步：备齐资料

**先把这一步做完再动手**，后面每一步都要用。资料分四类。

### 0.1 要找别人要的

| # | 要什么 | 它是什么 | 找谁要 | 用在 |
|---|---|---|---|---|
| 1 | **模型服务**：地址、模型名、密钥 | 大模型的接口（OpenAI 兼容）。地址形如 `http://<主机>/v1`，**必须以 `/v1` 结尾** | 模型服务团队（AI 平台管理员）。用公有云大模型时，在该云的控制台里创建 API 密钥 | 第 6 步 |
| 2 | **中间件**：主机、端口、接口路径前缀 | 智能体查数据库的入口。路径前缀以 `/` 开头、末尾不带 `/` | 中间件团队 | 第 6 步 |
| 3 | **中间件的签名要求**：Appkey、时间戳单位、是否要完整签名 | 中间件对调用方的校验方式，三种情况见下方 | 中间件团队 | 第 6 步 |
| 4 | **每个使用者的中间件令牌** | 中间件的个人访问令牌，中间件靠它识别是谁在查库 | 中间件团队，按工号签发。详见下方「令牌从哪来」 | 第 7 步 |
| 5 | **NAS**：NFS 服务器地址、导出目录 | 存会话、报告、知识库的共享存储 | 存储管理员。单节点测试环境可以不要，见第 5 步 | 第 5、6 步 |
| 6 | **平台侧的三件事**：建两个沙箱组；模板构建主机的 root 登录；确认该主机上有 `/root/.e2b/config.json` | 沙箱组见第 4 步。`config.json` 里是平台的 E2B 团队密钥，安装 E2B 时生成，注册模板要用 | 平台管理员 | 第 3、4 步 |

**中间件签名的三种情况**（问中间件团队「调用诊断接口要不要带 Appkey 和签名」）：

| 中间件的要求 | `GRMP_APPKEY` | `GRMP_SIGN_MODE` | 还要什么 |
|---|---|---|---|
| 不校验调用方 | 留空 | 不用管 | 无 |
| 只要 Appkey 和时间戳 | 填中间件给的 Appkey | `headers-only` | 无 |
| 要完整签名 | 填中间件给的 Appkey | `full` | 中间件签发的 SM2 私钥文件（第 6.3 步的可选项） |

**令牌从哪来**

- 令牌由中间件按工号签发。本产品不生成它，也没有命令能替你申请，只能找中间件团队要。
- 这套环境之前装过非容器版的技能：当时配在环境变量 `GRMP_AUTH_TOKEN` 里的值就是它，可以直接用。
- 测试环境的中间件只有一个公用令牌：每个测试工号都填这同一个。
- 暂时拿不到：先填一个占位值（例如 `placeholder`）。能登录、能对话、能查知识库，但登录数据库和诊断会失败，拿到真令牌后按第 7.1 步重建即可。
- 只用测试环境的令牌，不要填生产令牌。

### 0.2 自己用命令查的（在部署机上）

```bash
for c in kubectl openssl python3 curl; do command -v $c > /dev/null || echo "缺 $c"; done; echo 检查完
```
> 只有一行 `检查完`。提示缺哪个，就先请系统管理员装上。

```bash
uname -m
```
> `aarch64`。鲲鹏是 ARM64 架构，本文的安装包都按它选。

```bash
kubectl get pods -n flux-system; kubectl get pods -n e2b; kubectl api-resources | grep sandbox.flux.io
```
> 两个命名空间里的 Pod 都是 `Running`；最后一条列出 `sandboxes`、`sandboxgroups`、`subsandboxgroups`。
> 缺任何一样，说明平台的沙箱组件没装好，先找平台管理员。

```bash
kubectl -n flux-system get svc opensandbox-server
```
> 有这个服务，记下 `PORT(S)` 里的端口（一般是 `80`）。**OpenSandbox 地址** = `http://opensandbox-server.flux-system.svc:<端口>`。

```bash
kubectl get nodes -o jsonpath='{range .items[*]}{.spec.podCIDR}{"\n"}{end}'
```
> 每个节点一行，形如 `10.244.0.0/24`。**Pod 网段**取它们共同的大网段：前两段不变，写成 `10.244.0.0/16`。拿不准就问集群管理员「Pod 网段是多少」。

```bash
timedatectl | grep synchronized
```
> `System clock synchronized: yes`。是 `no` 就先请系统管理员配好时钟同步，否则资源大盘的统计读不到。

### 0.3 不用准备、安装时自动生成的

网关信任凭据、运维口令、沙箱控制密钥。第 6.3 步的命令会生成并保存，不用提前准备。

### 0.4 自己定的

两个测试工号：一个普通用户（本文用 `u1001`），一个管理员（本文用 `u2001`，既是知识库管理员，也能看资源大盘）。
工号只能用**小写字母、数字、连字符**。

---

## 第 1 步：下载安装包并校验

在能上外网的电脑上，打开发布页 <https://github.com/sqlrush/gh_gs_k8s_agent/releases/tag/agent-v1.5.2>，下载这 7 个文件（共约 1.3 GB）：

| 文件 | 大小 | 内容 |
|---|---|---|
| `gaussdb-agent-v1.5.2-oc1.18.27-arm64.tar.gz` | 459 MB | 5 个 K8s 镜像 |
| `gaussdb-agent-v1.5.2-oc1.18.27-e2b-arm64.oci.tar` | 843 MB | 2 个沙箱模板镜像 |
| `gaussdb-agent-v1.5.2-oc1.18.27-k8s.tar.gz` | 1 MB | 部署清单、小工具和全部手册 |
| 以上三个文件各自的 `.sha256` | 很小 | 校验值 |
| `e2b-agent-v1.5.2.tar.gz` | 很小 | 模板注册脚本 |

用命令下载也可以：

```bash
B=https://github.com/sqlrush/gh_gs_k8s_agent/releases/download/agent-v1.5.2
P=gaussdb-agent-v1.5.2-oc1.18.27
for f in $P-arm64.tar.gz $P-e2b-arm64.oci.tar $P-k8s.tar.gz; do curl -fLO $B/$f; curl -fLO $B/$f.sha256; done
curl -fLO $B/e2b-agent-v1.5.2.tar.gz
```

把 7 个文件拷到部署机的同一个目录（本文用 `/root/gsdb`），**拷完先校验**：

```bash
mkdir -p /root/gsdb && cd /root/gsdb
sha256sum -c *.sha256
```
> 三行都是 `OK`。

**有一行不是 `OK` 就停下，重新传那个文件。** 文件在传输途中损坏是最常见的问题：大小看起来对，内容却可能是坏的，后面的导入会报 `unrecognized image format` 之类看不懂的错。每换一台机器都重新校验一次。

---

## 第 2 步：导入 5 个镜像（每个集群节点各做一次）

`kubectl get nodes` 列出几个节点，就在几个节点上各做一次（先把 `…-arm64.tar.gz` 和它的 `.sha256` 拷过去并校验）：

```bash
cd /root/gsdb
gunzip -c gaussdb-agent-v1.5.2-oc1.18.27-arm64.tar.gz | ctr -n k8s.io images import -
```
> 若干行 `unpacking … done`，没有 `error`。

```bash
ctr -n k8s.io images ls -q | grep gaussdb-agent
```
> 5 行：`runtime`、`kb-import`、`gateway`、`frontend`、`kbstd`，标签都是 `agent-v1.5.2-oc1.18.27`。

`-n k8s.io` 不能省：Kubernetes 只认这个命名空间里的镜像，省了会导到别处，Pod 会报找不到镜像。

> 有内网镜像仓库、想让节点从仓库拉镜像的，做法见《Fuyao沙箱部署手册》§3，本文不展开。

---

## 第 3 步：注册 2 个沙箱模板（模板构建主机，root）

模板构建主机不是部署机时，先把 `gaussdb-agent-v1.5.2-oc1.18.27-e2b-arm64.oci.tar`、它的 `.sha256` 和 `e2b-agent-v1.5.2.tar.gz` 拷到那台主机的 `/root/gsdb`，并再校验一次。

```bash
cd /root/gsdb
tar xzf e2b-agent-v1.5.2.tar.gz
ls -l /root/.e2b/config.json
```
> 列出这个文件。提示不存在就找平台管理员。**不用打开它**，注册脚本自己会读。

```bash
python3 e2b/scripts/build-e2b-template.py --tag agent-v1.5.2-oc1.18.27 --arch arm64 \
    --from-oci gaussdb-agent-v1.5.2-oc1.18.27-e2b-arm64.oci.tar --cpu 2 --memory 4096
```
> 先后出现下面四行，中间各隔几分钟：

```text
模板构建:注册模板 gsdb-runtime-v1-5-2(…,2 vCPU / 4096 MB)…
模板构建:模板 gsdb-runtime-v1-5-2 就绪(…秒,templateID …)
模板构建:注册模板 gsdb-import-v1-5-2(…)…
模板构建:模板 gsdb-import-v1-5-2 就绪(…秒,templateID …)
```

每个模板实测 1 到 6 分钟。得到两个模板名：**`gsdb-runtime-v1-5-2`**（诊断）、**`gsdb-import-v1-5-2`**（导入），第 6 步的配置里已经写好，不用再填。

注册期间这台主机的负载会明显升高，**避开别人正在用平台的时段**。脚本可以重复执行。

| 报错 | 处理 |
|---|---|
| `建模板 … 失败(HTTP 401/403)` | `/root/.e2b/config.json` 里的密钥不对或过期，找平台管理员 |
| 提示 `kubectl` 找不到或连不上 | 这台主机不能执行 `kubectl`：向平台管理员要 E2B API 地址，命令末尾加 `--e2b-api http://<E2B API 地址>:3000` |
| `OCI 目录 … 里应正好一个 …` | 文件没传完整，回第 1 步重新校验 |
| `构建超时(1800 秒)` | 平台的构建服务卡住了：`kubectl -n e2b get pods` 看状态，请平台管理员处理后重跑 |

---

## 第 4 步：建 2 个沙箱组（平台管理员，部署机）

下面这份定义的容量是：**同时 8 人诊断、2 位管理员导入知识库**，在节点上预留约 8 核 CPU、40 GiB 内存（加上我方组件共约 10 核、42 GiB）。
暂停的沙箱不占名额，所以按「同时在用的人数」算，不按总人数算。要更大的容量，算法见《Fuyao沙箱部署手册》§1.4。

```bash
cd /root/gsdb
cat > sandboxgroups-site.yaml <<'EOF'
apiVersion: sandbox.flux.io/v1alpha1
kind: SandboxGroup
metadata:
  name: sg-gsdb-runtime
  namespace: flux-system
  labels: {app.kubernetes.io/part-of: gaussdb-agent}
spec:
  capacity: {agentPodMin: 1, agentPodMax: 1, maxSandboxesPerPod: 8, sandboxBufferMin: 0}
  sandboxResources:
    requests: {cpu: 500m, memory: 4Gi}
    limits: {cpu: "2", memory: 4Gi}
---
apiVersion: sandbox.flux.io/v1alpha1
kind: SandboxGroup
metadata:
  name: sg-gsdb-import
  namespace: flux-system
  labels: {app.kubernetes.io/part-of: gaussdb-agent}
spec:
  capacity: {agentPodMin: 1, agentPodMax: 1, maxSandboxesPerPod: 2, sandboxBufferMin: 0}
  sandboxResources:
    requests: {cpu: "2", memory: 4Gi}
    limits: {cpu: "2", memory: 4Gi}
EOF
kubectl apply -f sandboxgroups-site.yaml
```
> `sandboxgroup.sandbox.flux.io/sg-gsdb-runtime created`、`…/sg-gsdb-import created`。

等一两分钟再查：

```bash
kubectl -n flux-system get ssg | grep -E 'NAME|sg-gsdb-'
kubectl -n flux-system get pods | grep 'sandbox-agent-pod-sg-gsdb-'
```
> 第一条：`sg-gsdb-runtime-0` 的 `PODS` 为 1、`CAPACITY` 为 8；`sg-gsdb-import-0` 为 1 和 2。
> 第二条：两个 Pod 都是 `2/2 Running`。

Pod 一直 `Pending`：节点的 CPU 或内存不够预留，`kubectl -n flux-system describe pod <Pod 名>` 看最后几行事件确认。
处理：`kubectl delete -f sandboxgroups-site.yaml` 删掉这两个组（这时里面还没有沙箱，删掉没有影响），把文件里的 `maxSandboxesPerPod` 改小，再 `kubectl apply -f sandboxgroups-site.yaml`。

---

## 第 5 步：准备 NAS

二选一。

**情形 A：有 NFS**（多节点集群必须用这种）。请存储管理员在 NFS 服务器上执行，然后把**服务器地址**和**导出目录**告诉你：

```bash
mkdir -p /export/gaussdb-agent/{users,admins,kb}
chown -R 1000:1000 /export/gaussdb-agent
chmod -R 775 /export/gaussdb-agent
```

**情形 B：单节点测试环境，没有 NFS**。在这个节点上建一个本机目录代替：

```bash
mkdir -p /srv/gaussdb-agent/nas/{users,admins,kb}
chown -R 1000:1000 /srv/gaussdb-agent/nas
chmod -R 775 /srv/gaussdb-agent/nas
```

两种情形下，目录的属主都必须是 `1000:1000`（我方组件以这个用户运行），否则能对话但会话存不下来。
情形 B 只能用于只有一个节点的集群：数据只在这一台机器上。

---

## 第 6 步：填参数、建密钥、部署（部署机）

这一步的命令都在 `bash` 里、`/root/gsdb/deploy` 目录下执行。

### 6.1 解开清单包

```bash
cd /root/gsdb && mkdir -p deploy && tar xzf gaussdb-agent-v1.5.2-oc1.18.27-k8s.tar.gz -C deploy && cd deploy
ls
```
> `docs  k8s`。

### 6.2 把第 0 步的资料填进参数文件

先生成参数文件，再打开它，**把带「填」字的地方都换成真实的值**，其余按注释核对：

```bash
cat > site.env <<'EOF'
# ---- 平台（第 0.2 步查到的）----
FUYAO_API=http://opensandbox-server.flux-system.svc:80
POD_CIDR=10.244.0.0/16

# ---- 模型服务（模型服务团队给的；密钥不写在这里，第 6.3 步单独输入）----
MODEL_BASE_URL=http://填模型服务地址/v1
MODEL_ID=填模型名
MODEL_NET=10.0.0.0/8

# ---- 中间件（中间件团队给的；令牌不写在这里，第 7.1 步单独输入）----
GRMP_API_HOST=填中间件主机名或IP
GRMP_API_PORT=8080
GRMP_PATH_PREFIX=/填接口路径前缀
GRMP_APPKEY=
GRMP_SIGN_MODE=headers-only
GRMP_SIGN_TIMESTAMP=ms
GRMP_NET=10.0.0.0/8

# ---- NAS（第 5 步）：情形 A 写 nfs，并改下面两项 NFS_；情形 B 写 local ----
NAS_MODE=nfs
NFS_SERVER=填NFS服务器地址
NFS_PATH=/export/gaussdb-agent
LOCAL_NAS_DIR=/srv/gaussdb-agent/nas

# ---- 自己定的 ----
TEST_USER=u1001
ADMIN_USER=u2001
PRODUCT_NAME="GaussDB 智能体"
EOF
vi site.env
```

| 参数 | 怎么填 |
|---|---|
| `FUYAO_API`、`POD_CIDR` | 第 0.2 步查到的 OpenSandbox 地址、Pod 网段 |
| `MODEL_BASE_URL`、`MODEL_ID` | 模型服务的地址（以 `/v1` 结尾）和模型名 |
| `GRMP_API_HOST`、`GRMP_API_PORT`、`GRMP_PATH_PREFIX` | 中间件的主机、端口、接口路径前缀 |
| `GRMP_APPKEY`、`GRMP_SIGN_MODE` | 按第 0.1 步「中间件签名的三种情况」填。不校验就保持 `GRMP_APPKEY=` 为空 |
| `GRMP_SIGN_TIMESTAMP` | 时间戳单位：`ms` 毫秒，`s` 秒，问中间件团队。`GRMP_APPKEY` 为空时不起作用 |
| `MODEL_NET`、`GRMP_NET` | 模型服务、中间件的 IP 所在网段，例如 `10.1.2.3/32`。网络插件是 Flannel 时不起作用，不用改；其他插件见 6.6 |
| `NAS_MODE` 等 | 第 5 步选的哪种情形。情形 B 写 `NAS_MODE=local`，两项 `NFS_` 不用管 |
| `TEST_USER`、`ADMIN_USER` | 两个测试工号，小写 |
| `PRODUCT_NAME` | 页面上显示的产品名，保留两边的引号 |

改完检查有没有漏填：

```bash
grep -v '^#' site.env | grep '填' || echo "都填好了"
```
> `都填好了`。`NAS_MODE=local` 时 `NFS_SERVER` 那一行还带「填」字是正常的。

### 6.3 建命名空间和密钥

```bash
kubectl create namespace gaussdb-agent
kubectl -n gaussdb-agent create secret generic gateway-auth \
  --from-literal=TRUST_SECRET="$(openssl rand -hex 24)" --from-literal=ADMIN_TOKEN="$(openssl rand -hex 24)"
kubectl -n gaussdb-agent create secret generic sandbox-control --from-literal=KEY="$(openssl rand -hex 32)"
```
> 一行 `namespace/gaussdb-agent created`，两行 `secret/… created`。

把信任凭据和运维口令各存一份到本机文件（只有你能读），第 7 步验证时用：

```bash
(umask 077
 kubectl -n gaussdb-agent get secret gateway-auth -o jsonpath='{.data.TRUST_SECRET}' | base64 -d > ~/.agent-trust
 kubectl -n gaussdb-agent get secret gateway-auth -o jsonpath='{.data.ADMIN_TOKEN}'  | base64 -d > ~/.agent-admin)
wc -c ~/.agent-trust ~/.agent-admin
```
> 两个文件都是 48 个字符。

模型服务密钥**分两次执行**。先单独执行下面这一行，出现提示后粘贴密钥、回车（屏幕上不显示）：

```bash
read -rsp "模型服务密钥: " MK; echo
```

再执行：

```bash
kubectl -n gaussdb-agent create secret generic model-api --from-literal=MODEL_API_KEY="$MK"; unset MK
kubectl -n gaussdb-agent get secret
```
> 列出 `gateway-auth`、`sandbox-control`、`model-api` 三个。**不要加 `-o yaml`**，那会把密钥打印到屏幕上。

可选项，用得到才做：

| 什么时候需要 | 命令 |
|---|---|
| 中间件要完整签名（`GRMP_SIGN_MODE=full`） | `kubectl -n gaussdb-agent create secret generic grmp-sm2 --from-file=GRMP_SM2_PRIVATE_KEY=<中间件签发的私钥文件>` |
| 平台给 OpenSandbox 开了认证 | 向平台管理员要 API Key，仿照上面模型密钥的两步做法，建 `fuyao-api`，键名 `API_KEY` |

### 6.4 生成现场配置

下面五段各生成一个文件，**逐段复制执行**，前四段没有输出。

```bash
. ./site.env; S=k8s/overlays/site; mkdir -p $S
cat > $S/configmap-gateway-site.yaml <<EOF
apiVersion: v1
kind: ConfigMap
metadata: {name: gateway-config, namespace: gaussdb-agent}
data:
  FUYAO_API: "$FUYAO_API"
  FUYAO_RUNTIME_GROUP: "sg-gsdb-runtime"
  FUYAO_IMPORT_GROUP: "sg-gsdb-import"
  FUYAO_RUNTIME_TEMPLATE: "gsdb-runtime-v1-5-2"
  FUYAO_IMPORT_TEMPLATE: "gsdb-import-v1-5-2"
  PRODUCT_NAME: "$PRODUCT_NAME"
EOF
```

```bash
. ./site.env; S=k8s/overlays/site
cat > $S/configmap-agent-site.yaml <<EOF
apiVersion: v1
kind: ConfigMap
metadata: {name: agent-config, namespace: gaussdb-agent}
data:
  GRMP_API_HOST: "$GRMP_API_HOST"
  GRMP_API_PORT: "$GRMP_API_PORT"
  GRMP_PATH_PREFIX: "$GRMP_PATH_PREFIX"
  GRMP_APPKEY: "$GRMP_APPKEY"
  GRMP_SIGN_MODE: "$GRMP_SIGN_MODE"
  GRMP_SIGN_TIMESTAMP: "$GRMP_SIGN_TIMESTAMP"
  MODEL_BASE_URL: "$MODEL_BASE_URL"
  MODEL_ID: "$MODEL_ID"
EOF
```

```bash
. ./site.env; S=k8s/overlays/site
cat > $S/configmap-agent-roles-site.yaml <<EOF
apiVersion: v1
kind: ConfigMap
metadata: {name: agent-roles, namespace: gaussdb-agent}
data:
  kb-admins: |
    $ADMIN_USER
  ops-admins: |
    $ADMIN_USER
EOF
```

```bash
. ./site.env; S=k8s/overlays/site
if [ "$NAS_MODE" = local ]; then
cat > $S/nas-pv.yaml <<EOF
apiVersion: v1
kind: PersistentVolume
metadata: {name: nas-local}
spec:
  capacity: {storage: 500Gi}
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: nas
  claimRef: {namespace: gaussdb-agent, name: nas}
  hostPath: {path: "$LOCAL_NAS_DIR", type: Directory}
EOF
else
cat > $S/nas-pv.yaml <<EOF
apiVersion: v1
kind: PersistentVolume
metadata: {name: nas-nfs}
spec:
  capacity: {storage: 500Gi}
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: nas
  claimRef: {namespace: gaussdb-agent, name: nas}
  mountOptions: [nfsvers=4.1, hard, local_lock=all]
  nfs: {server: "$NFS_SERVER", path: "$NFS_PATH"}
EOF
fi
```

```bash
. ./site.env; S=k8s/overlays/site
cat > $S/kustomization.yaml <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../fuyao
  - nas-pv.yaml
patches:
  - path: configmap-gateway-site.yaml
  - path: configmap-agent-site.yaml
  - path: configmap-agent-roles-site.yaml
  - target: {kind: NetworkPolicy, name: gateway-to-sandboxes}
    patch: |-
      - {op: replace, path: /spec/egress/0/to/0/ipBlock/cidr, value: $POD_CIDR}
  - target: {kind: NetworkPolicy, name: statesvc-policy}
    patch: |-
      - {op: replace, path: /spec/ingress/0/from/0/ipBlock/cidr, value: $POD_CIDR}
  - target: {kind: NetworkPolicy, name: credproxy-policy}
    patch: |-
      - {op: replace, path: /spec/ingress/0/from/0/ipBlock/cidr, value: $POD_CIDR}
      - {op: replace, path: /spec/egress/1, value: {to: [{ipBlock: {cidr: $GRMP_NET}}], ports: [{port: $GRMP_API_PORT, protocol: TCP}]}}
      - {op: replace, path: /spec/egress/2, value: {to: [{ipBlock: {cidr: $MODEL_NET}}]}}
EOF
ls $S
```
> 五个文件：`configmap-agent-roles-site.yaml`、`configmap-agent-site.yaml`、`configmap-gateway-site.yaml`、`kustomization.yaml`、`nas-pv.yaml`。

以后改了 `site.env`，把 6.4、6.5 重做一遍即可。

### 6.5 部署

```bash
kubectl kustomize k8s/overlays/site > rendered.yaml
grep -E '^  (RUNTIME_MODE|FUYAO_API|FUYAO_(RUNTIME|IMPORT)_TEMPLATE|MODEL_BASE_URL|MODEL_ID|GRMP_API_HOST|GRMP_API_PORT|GRMP_PATH_PREFIX):' rendered.yaml
```
> 9 行：`RUNTIME_MODE: fuyao`、两个模板名，其余是你在 `site.env` 里填的值。值不对就回 6.2 改，再重做 6.4。

```bash
kubectl apply -f rendered.yaml
for d in statesvc credproxy gateway frontend; do kubectl -n gaussdb-agent rollout status deploy/$d --timeout=240s; done
```
> 一串 `created` / `configured`，然后四行 `deployment "…" successfully rolled out`。
> 命名空间那一行出现 `Warning: … missing the kubectl.kubernetes.io/last-applied-configuration annotation` 是正常的。

```bash
kubectl -n gaussdb-agent get pods,pvc
```
> `gateway` 2 个、`frontend` 1 个、`statesvc` 1 个、`credproxy` 2 个，都是 `Running`；`kbstd-install-…` 是 `Completed`（官方文档集已装进知识库，要等一两分钟）；`nas` 是 `Bound`。

**apply 时报 `clusterroles … is forbidden`**：你的账号没有集群级权限。其他对象已经建好，只差两个只读授权（给资源大盘看沙箱组用）：把 `rendered.yaml` 交给平台管理员再执行一次 `kubectl apply -f rendered.yaml`。不做也能用，只是资源大盘里沙箱组那两块显示「需授权只读」。

### 6.6 自检：从集群里连一下模型服务和中间件

```bash
kubectl -n gaussdb-agent exec deploy/credproxy -- sh -c '
  curl -s -m 10 -o /dev/null -w "模型服务 %{http_code}\n" $MODEL_BASE_URL/models
  curl -s -m 10 -o /dev/null -w "中间件   %{http_code}\n" http://$GRMP_API_HOST:$GRMP_API_PORT/'
```
> 两行都是三位数字的状态码，`200`、`401`、`404` 都算通，说明网络到得了。

出现 `000` 就是不通，依次检查：

1. `site.env` 里的地址、端口有没有填错。
2. 在部署机上直接执行同样的 `curl` 通不通。也不通，说明是服务器之间的网络或防火墙问题，找网络管理员。
3. 部署机通、集群里不通，而且网络插件不是 Flannel（`kubectl get pods -A | grep -Ei 'calico|cilium'` 有输出）：把 `site.env` 的 `MODEL_NET`、`GRMP_NET` 改成模型服务、中间件的实际地址（如 `10.1.2.3/32`），重做 6.4、6.5。

---

## 第 7 步：开通用户并验证（部署机）

这一步接着第 6 步的终端做。新开的终端先执行 `cd /root/gsdb/deploy && . ./site.env`。

### 7.1 开通两个测试用户

**开通一个用户 = 给他建一个存中间件令牌的 Secret**，名字是 `grmp-token-<工号>`。令牌的来源见第 0.1 步「令牌从哪来」。

先单独执行这一行，出现提示后粘贴令牌、回车（屏幕上不显示）：

```bash
read -rsp "中间件令牌: " TK; echo
```

再执行（两个测试工号用同一个令牌）：

```bash
. ./site.env
for u in $TEST_USER $ADMIN_USER; do
  kubectl -n gaussdb-agent create secret generic grmp-token-$u --from-literal=GRMP_AUTH_TOKEN="$TK"
done; unset TK
```
> 两行 `secret/grmp-token-… created`。

两个工号各有各的令牌，或者以后给新同事开通：上面两段每人各做一次，把 `for u in $TEST_USER $ADMIN_USER` 换成 `for u in <工号>`。
令牌换了：先 `kubectl -n gaussdb-agent delete secret grmp-token-<工号>`，再重建。

### 7.2 用命令验证

```bash
kubectl -n gaussdb-agent port-forward svc/gateway 18089:80 > /tmp/gsdb-pf.log 2>&1 &
sleep 3; curl -s http://127.0.0.1:18089/healthz; echo
```
> `{"ok": true, "version": "gaussdb-agent-gateway"}`。

```bash
curl -s -H "X-Admin-Token: $(cat ~/.agent-admin)" http://127.0.0.1:18089/admin/fuyao; echo
```
> 一段 JSON，里面有 `"mode": "fuyao"` 和两个模板名，`sandboxes` 是空的。

```bash
curl -s -o /dev/null -w "HTTP %{http_code}  用时 %{time_total} 秒\n" \
  -H "X-Agent-User: $TEST_USER" -H "X-Agent-Trust: $(cat ~/.agent-trust)" http://127.0.0.1:18089/session
```
> `HTTP 200`，约 5 到 6 秒：网关为这个工号建好了第一台沙箱。再执行一次只要 1 到 2 秒。

| 看到 | 原因和处理 |
|---|---|
| `HTTP 403` | 信任凭据不对：确认 `~/.agent-trust` 是第 6.3 步生成的那个 |
| `HTTP 503` | 这个工号没开通：做 7.1 |
| 等了 90 秒以上才返回 | `kubectl -n gaussdb-agent logs -l app=agent-gateway --tail=50 \| grep Fuyao` 看原因：模板名不存在（第 3 步）、沙箱组满了或没建好（第 4 步） |

### 7.3 用浏览器验证

统一认证还没接，用包里的小工具模拟它（只用于测试，要求 Python 3.8 及以上，`python3 --version` 查看）。每个工号起一个，监听不同的端口：

```bash
python3 k8s/tools/sso-sim.py 18090 18089 $TEST_USER  ~/.agent-trust > /tmp/gsdb-sso1.log 2>&1 &
python3 k8s/tools/sso-sim.py 18091 18089 $ADMIN_USER ~/.agent-trust > /tmp/gsdb-sso2.log 2>&1 &
```

小工具只允许本机访问。浏览器不在部署机上时，在**你自己的电脑**上开一个终端，执行下面这行并保持窗口打开，再用你电脑上的浏览器访问：

```bash
ssh -L 18090:127.0.0.1:18090 -L 18091:127.0.0.1:18091 root@<部署机地址>
```

| 用谁的身份 | 浏览器打开 | 应该看到 |
|---|---|---|
| 普通用户 | `http://127.0.0.1:18090/runtime/` | 对话页。输入「登录 `<实例IP>` 的 `<库名>`」，回答已登录和主备状态；再输入「做一次健康检查」，得到完整结论 |
| 管理员 | `http://127.0.0.1:18091/dash/resources` | 资源大盘：两个沙箱组的容量、正在运行的沙箱 |
| 管理员 | `http://127.0.0.1:18091/kb-import/` | 知识库导入环境的对话页 |

「登录数据库」这一步失败、提示中间件拒绝或超时：令牌不对（7.1 重建），或第 6.6 步的自检没通过。

你的电脑连部署机不允许端口转发时，7.2 的命令验证通过即可，浏览器验证等统一认证接好后再做。

### 7.4 收尾

```bash
pkill -f 'port-forward svc/gateway 18089:80'; pkill -f 'k8s/tools/sso-sim.py'
```
> 没有输出：端口转发和两个模拟小工具都停了。

`~/.agent-trust`（信任凭据）以后接统一认证时要交给统一认证团队，`~/.agent-admin`（运维口令）是运维接口用的。两个文件**不要发到聊天工具里**，交接后从部署机上删掉。

---

## 常见报错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `ctr: … unrecognized image format`，或 `tar -tf <文件>` 什么都不输出 | 文件在传输途中坏了 | 回第 1 步：`sha256sum -c *.sha256`，不是 `OK` 就重传 |
| Pod 是 `ErrImagePull` / `ImagePullBackOff` | 这个节点没导入镜像，或导入时漏了 `-n k8s.io` | 在 Pod 所在的节点上重做第 2 步 |
| `credproxy` 是 `CreateContainerConfigError` | 没建 `model-api` | 做第 6.3 步的模型服务密钥 |
| `statesvc` 是 `CreateContainerConfigError` | 没建 `sandbox-control` | 做第 6.3 步 |
| `statesvc` 一直 `Pending` 或 `ContainerCreating` | NAS 没接上 | `kubectl -n gaussdb-agent get pvc nas` 不是 `Bound`：核对 `site.env` 的 NAS 几项和第 5 步的目录 |
| 网关反复重启 | 配置缺项 | `kubectl -n gaussdb-agent logs -l app=agent-gateway --tail=20`，日志里用中文写明缺哪一项 |
| 登录后一直「正在为您准备环境」 | 模板名不存在、沙箱组没建好 | 网关日志里 `Fuyao:` 开头的行；核对第 3、4 步 |
| 「当前使用人数已满」 | 沙箱组的名额用完了 | 等别人闲置（默认 30 分钟后自动暂停让出名额），或请平台管理员把第 4 步的容量调大 |
| 「您的环境尚未开通」 | 这个工号没有令牌 Secret | 做第 7.1 步 |
| 能对话，但登录数据库失败 | 令牌不对，或中间件地址、路径前缀、签名配置不对 | 核对第 0.1 步的第 2 到 4 项；改了 `site.env` 后重做 6.4、6.5，再 `kubectl -n gaussdb-agent rollout restart deploy/credproxy` |
| 对话卡住很久后超时 | 模型服务不通 | 做第 6.6 步的自检 |
| 资源大盘显示「需授权只读」 | 两个只读授权没建上 | 见第 6.5 步末尾 |

改了 `agent-config` 里的内容（中间件、模型服务的地址等）之后，**已经建好的沙箱不会自动更新**：重启凭据代理后，还要让用户的沙箱重建，做法见《Fuyao沙箱部署手册》§12.1。

查问题时最常用的三条命令：

```bash
kubectl -n gaussdb-agent get pods
kubectl -n gaussdb-agent logs -l app=agent-gateway --tail=50
kubectl -n gaussdb-agent logs deploy/statesvc --tail=50
```

---

## 装完之后

| 想做什么 | 看哪里 |
|---|---|
| 接入统一认证，让用户不用小工具直接登录 | 《Fuyao沙箱部署手册》§9 |
| 给更多人用：加大沙箱组容量、调整每台沙箱的规格 | 《Fuyao沙箱部署手册》§1.4、§11 |
| 让智能体认得「核心库」这类叫法，不用每次报 IP | 《参数手册》「实例别名」 |
| 升级到新版本 | 《Fuyao沙箱部署手册》§10 |
| 日常运维：看有哪些沙箱、暂停、删除 | 《Fuyao沙箱部署手册》§12 |
| 卸载 | 《Fuyao沙箱部署手册》§14 |
| 了解用于生产之前平台必须落实的三项安全前提 | 《Fuyao沙箱部署手册》§1.1、§15 |
