# 从源码构建到 K8s 运行：完整手册

从 GitHub 拉代码开始，自己构建 4 个应用镜像（第 5 个标准知识库镜像由我方发布，从镜像仓或离线包取），推进镜像仓库，部署到 Kubernetes，
最后在浏览器里登录、让模型对数据库做一次健康检查、在大盘上看到结果。**每一步都给出命令和执行后应该看到的输出。**

> **本手册的每条命令都实际跑过。** 2026-10-15 在一台全新的机器目录里从 GitHub clone，
> 用不带任何缓存的构建器从零构建，部署到一个全新的 kind 集群（单节点，Kubernetes 1.36，执行 NetworkPolicy），
> 一直走到浏览器里出健康检查大盘。输出都是那次的原样；`<...>` 是需要你换成自己的值的地方。
> x86_64（amd64）与 ARM（arm64）两种架构的镜像都构建并验证过。验证记录见附录 C。
>
> **当前发布版本 `agent-v1.5.3`（2026-11-19）。** 第 2 章 `git checkout agent-v1.5.3` 后按本手册构建，`TAG` 就是 `agent-v1.5.3-oc1.18.27`；
> 第 3 章的镜像验证输出按 v1.3.0 重跑（之后各版构建步骤相同；v1.3.2 起镜像里多了 sqlite-vec，导入镜像多了 pillow / pillow-heif；v1.4.2 起多了第 5 个镜像——标准知识库，不从源码构建，见 3.5）。**不想自己构建**的，直接拉已发布的镜像（两种架构）：
> `ghcr.io/sqlrush/gaussdb-agent-{runtime,kb-import,gateway,frontend,kbstd}:agent-v1.5.3-oc1.18.27`，从第 4 章开始。
> 部署前还要让**中间件团队导入诊断脚本白名单**（本版 137 条，见 1.3 与 5.0）。

| 你要做的 | 章节 | 大约用时 |
|---|---|---|
| 准备构建机、集群、要填的值 | 1 | 看各团队响应速度 |
| 拉代码 | 2 | 1 分钟 |
| 构建 4 个应用镜像 | 3 | 2–10 分钟（看网络） |
| 推进镜像仓库 | 4 | 1–5 分钟 |
| 部署到 K8s、开通工号、浏览器里用起来 | 5 | 15–30 分钟 |

---

## 0. 全景

### 0.1 从代码到运行的全过程

```mermaid
flowchart LR
    GH["GitHub 仓库<br/>sqlrush/gh_gs_k8s_agent"] -->|git clone| BUILD
    subgraph BUILD["构建机 (docker buildx)"]
        B1["scripts/build-images.sh<br/>runtime · kb-import · gateway · frontend"]
    end
    BUILD -->|docker push| REG["镜像仓库<br/>(贵方内网仓库 / Harbor 等)"]
    REG -->|节点拉镜像| K8S["Kubernetes 集群<br/>命名空间 gaussdb-agent"]
    KUBECTL["跳板机 kubectl<br/>k8s/ 清单"] -->|kubectl apply| K8S
```

### 0.2 运行起来之后是什么样

```mermaid
flowchart LR
    U["用户浏览器"] --> SSO["SSO / 反向代理<br/>认人，注入工号头 + 信任凭据"]
    SSO --> GW["gateway ×2<br/>认工号 · 按工号建/收容器 · 反向代理"]
    GW -->|/dash/*| FE["frontend<br/>四个大盘的静态页面"]
    GW -->|"对话、报告"| RT["runtime-{工号}<br/>每人一个，登录时创建"]
    GW -.->|知识库管理员另建| KI["kb-import-{工号}"]
    RT --> GRMP["中间件 GRMP"] --> DB[("被诊断的数据库")]
    RT --> LLM["模型服务<br/>(OpenAI 兼容接口)"]
    RT --- NAS[("NAS (RWX 卷)<br/>users/{工号} · kb/(含索引 index/store.sqlite)")]
    KI --- NAS
```

### 0.3 五个镜像

| # | 镜像 | 从哪来 | 里面是什么 | 大小（arm64 实测） | 在集群里的形态 |
|---|---|---|---|---|---|
| 1 | `gaussdb-agent-runtime` | `docker/Dockerfile` 的 `runtime` 阶段 | opencode 1.18.27 + 18 个诊断技能 + 知识库只读查询（含 sqlite-vec） | 674 MB | 每个工号一个，登录时由网关创建 |
| 2 | `gaussdb-agent-kb-import` | `docker/Dockerfile` 的 `kb-import` 阶段 | opencode + 知识库查询 / 导入 / 入库前标准化 3 个技能（含 sqlite-vec，写索引） | 674 MB | 只给知识库管理员，每人一个 |
| 3 | `gaussdb-agent-gateway` | `docker/Dockerfile.gateway` | 网关（Python 标准库 + PyYAML），带用户 Pod 模板 | 227 MB | 常驻 2 副本 |
| 4 | `gaussdb-agent-frontend` | `docker/Dockerfile.frontend` | nginx（非 root）+ `frontend/` 静态文件 | 78.4 MB | 常驻 1 副本 |
| 5 | `gaussdb-agent-kbstd` | 我方发布（镜像仓 / 离线包），**不从源码构建**：内容是 GaussDB 官方文档的派生，不进代码仓库 | 标准知识库包（官方文档集 G25132C：GaussDB 25.1.32 集中式，6023 条，含检索索引） | 约 370 MB（基础层与导入镜像共用，实际多下载约 250 MB） | 部署时一次性 Job `kbstd-install-*` 把它装进 NAS 的 `kb/docsets/`，装完退出 |

```mermaid
flowchart TB
    subgraph DF["docker/Dockerfile (一个文件两个产物)"]
        BASE["base 阶段<br/>python:3.12-slim · apt 工具 · opencode 二进制<br/>models.json · Python 依赖 · 安装全部技能 · 启动脚本"]
        BASE --> R["runtime 阶段<br/>删掉知识库导入/标准化技能，断言剩 18 个"]
        BASE --> K["kb-import 阶段<br/>只留知识库 3 个技能，断言剩 3 个"]
    end
    G["docker/Dockerfile.gateway<br/>python:3.12-slim + gateway/ + k8s/templates/"]
    F["docker/Dockerfile.frontend<br/>nginx-unprivileged + frontend/"]
```

---

## 1. 准备

### 1.1 构建机

一台 Linux（x86_64 或 ARM 都行）或 macOS 机器：

| 要求 | 怎么确认 |
|---|---|
| Docker 20.10 以上，带 buildx | `docker buildx version` 有输出 |
| git、curl | `git --version`、`curl --version` |
| 磁盘剩余 20 GB 以上 | `df -h` |
| 能访问外网的这些地址（构建时下载） | 见下表 |

| 构建时访问的地址 | 用途 |
|---|---|
| `github.com` | 拉代码；下载 opencode 二进制 |
| Docker Hub（`registry-1.docker.io`） | 基础镜像 `python:3.12-slim-bookworm`、`nginxinc/nginx-unprivileged` |
| `deb.debian.org` | runtime 基础镜像里装 curl、git、ripgrep 等 |
| `pypi.org` / `files.pythonhosted.org` | psycopg2-binary、cryptography、PyYAML、sqlite-vec |
| `models.dev` | opencode 的模型清单（构建期取好，运行时不再联网取） |

> **构建机要经代理上网时**：在 `~/.docker/config.json` 里配 `proxies`，Docker 会把它自动传进每一步构建。
> 构建机完全不能上外网时，4 个镜像需要一台能上网的机器构建后，`docker save` 带过去。

### 1.2 Kubernetes 集群

| 要求 | 说明 |
|---|---|
| Kubernetes 1.24 以上 | 本手册在 1.36 上验证 |
| 网络插件支持 NetworkPolicy | Calico / Cilium / kind 自带的 kindnet 都可以。不支持的话策略不生效（不报错，但等于没有隔离） |
| 一个 RWX 存储 | NFS，或集群已有的 RWX StorageClass。所有用户的会话、报告、知识库都在上面 |
| 节点能从镜像仓库拉镜像 | 节点访问第 4 章推镜像的那个仓库 |
| 一台装了 `kubectl` 的跳板机 | 有 `gaussdb-agent` 命名空间的管理权限；也要能 `git clone`（或把仓库目录拷过去） |

> **手边没有集群？** 附录 A 用 kind 在一台装了 Docker 的机器上起一个单节点集群和一个本地镜像仓库，
> 本手册的验证就是在那上面做的。

### 1.3 需要准备的值

| 值 | 找谁要 | 用在哪一步 |
|---|---|---|
| 镜像仓库地址（下文 `REG`），以及推送权限 | 容器平台管理员 | 4、5.1 |
| NFS 服务器地址、导出路径（或 RWX StorageClass 名） | 存储管理员 | 5.2 |
| 模型服务地址（OpenAI 兼容，一般以 `/v1` 结尾）、key、模型名 | AI 平台管理员 | 5.4、5.5 |
| 中间件（GRMP）的 IP 与端口 | 中间件团队 | 5.5、5.6 |
| 中间件要求签名时：Appkey 与 SM2 私钥 | 中间件团队 | 5.4、5.5 |
| 第一个要开通的工号，及该工号的中间件令牌 | 中间件团队 | 5.9 |
| **诊断脚本白名单已导入**（本版 118 条；已导入过上一版 105 条的只导增量，新增 13、更新 9） | 中间件团队 | 5.0 |

---

## 2. 拉代码

```bash
git clone https://github.com/sqlrush/gh_gs_k8s_agent.git
cd gh_gs_k8s_agent
git log --oneline -1
```

> 进入目录。**之后所有命令都在这个目录里执行。** 最后一行是当前版本，例如：
> ```
> 292a4b0 fix(gateway): 用户 Pod 镜像支持仓库前缀 USER_IMAGE_REGISTRY;修三份手册改镜像地址的两处错误
> ```

要构建某个发布版本，再执行 `git checkout <版本标签>`（`git tag` 列出全部标签）。不切就是最新的 `main`。
交付给客户的版本请切到发布标签：

```bash
git checkout agent-v1.4.3
```

和构建、部署有关的目录：

| 目录 | 内容 |
|---|---|
| `agent/` | 诊断技能的源码（runtime 与 kb-import 镜像把它装进 opencode 的技能目录） |
| `gateway/` | 网关源码 |
| `frontend/` | 大盘前端（纯静态文件，没有编译步骤） |
| `docker/` | 各镜像的 Dockerfile、容器启动脚本 |
| `k8s/base/` | 部署清单（命名空间、配置、网关、前端、网络策略） |
| `k8s/templates/` | 网关给每个用户建容器用的模板（构建网关镜像时拷进去） |
| `k8s/tools/` | 现场工具：`runtime-doctor.sh`（诊断/修复）、`sso-sim.py`（模拟 SSO） |
| `scripts/` | 构建脚本 `build-images.sh`、离线打包 `package-images.sh` |

定一个版本号，后面构建、推送、部署都用它：

```bash
TAG=$(git describe --tags --always)-oc1.18.27
echo $TAG
```

> 切在发布标签上时就是 `agent-v1.5.3-oc1.18.27`；不在标签上时形如 `agent-v1.4.3-2-g1a2b3c4-oc1.18.27`
> （最近的发布标签 + 之后的提交数 + 提交号）。`oc1.18.27` 是镜像里的 opencode 版本。
> 也可以自己定，例如 `TAG=v20261015-oc1.18.27`，只要后面一直用同一个值。

---

## 3. 构建 4 个应用镜像

### 3.1 一条命令

```bash
bash scripts/build-images.sh "$TAG"
```

最后一行：

```
built: gaussdb-agent-{runtime,kb-import,gateway,frontend}:agent-v1.5.3-oc1.18.27
```

验证时从零构建（不带缓存）用了 96 秒；网络慢时 5–10 分钟，主要花在下载上。

```bash
docker images | grep "gaussdb-agent.*$TAG"
```

> 4 行：
> ```
> gaussdb-agent-frontend    agent-v1.5.3-oc1.18.27   78.4MB
> gaussdb-agent-gateway     agent-v1.5.3-oc1.18.27   227MB
> gaussdb-agent-kb-import   agent-v1.5.3-oc1.18.27   674MB
> gaussdb-agent-runtime     agent-v1.5.3-oc1.18.27   674MB
> ```

### 3.2 这条命令做了什么

`build-images.sh` 依次执行 4 次 `docker buildx build`：

| 镜像 | 构建命令（脚本里） | 关键步骤 |
|---|---|---|
| runtime | `-f docker/Dockerfile --target runtime` | ① `python:3.12-slim-bookworm` 上装 curl、git、ripgrep，建 uid 1000 的用户 `agent` ② 下载 opencode 1.18.27 官方 release（按架构选 x64 / arm64）③ 预取模型清单 `models.json` ④ `pip install` psycopg2-binary、cryptography、PyYAML、sqlite-vec（当场加载一次 sqlite-vec，加载不了构建就失败），装完删掉 pip ⑤ 用 `agent/install-opencode.sh` 把技能装进 `/opt/agent/config/opencode/skills` ⑥ 拷启动脚本与 `docker/*.py` ⑦ 删掉知识库导入与标准化两个技能，**断言剩 18 个**，少一个构建就失败 |
| kb-import | `-f docker/Dockerfile --target kb-import` | ①–⑥ 与 runtime 共用（Docker 会复用这些层）⑦ 只留知识库 3 个技能，断言剩 3 个 |
| gateway | `-f docker/Dockerfile.gateway` | `python:3.12-slim` 上装 PyYAML，拷 `gateway/` 与 `k8s/templates/`，非 root（uid 1000）运行 |
| frontend | `-f docker/Dockerfile.frontend` | `nginx-unprivileged` 上拷 `frontend/` 与 nginx 配置，没有编译步骤 |

不带 `--platform` 时构建本机架构，并载入本机 Docker（`--load`）。

### 3.3 验证镜像内容

```bash
docker run --rm --entrypoint sh gaussdb-agent-runtime:$TAG -c 'opencode --version; ls -d /opt/agent/config/opencode/skills/gaussdb-* | wc -l; id'
```

> ```
> 1.18.27
> 18
> uid=1000(agent) gid=1000(agent) groups=1000(agent)
> ```

```bash
docker run --rm --entrypoint sh gaussdb-agent-kb-import:$TAG -c 'ls /opt/agent/config/opencode/skills | grep gaussdb'
```

> `gaussdb-kb`、`gaussdb-kb-import`、`gaussdb-kb-init` 三行。

```bash
docker run --rm gaussdb-agent-gateway:$TAG
```

> `配置错误,网关拒绝启动:缺 GATEWAY_NAMESPACE(网关要在哪个命名空间里建 Pod)` —— 正常：
> 网关缺配置时**拒绝启动**，说明镜像本身是好的。

### 3.4 构建两种架构并直接推进仓库（正式发布用）

集群节点是 x86 而构建机是 ARM（或反过来）时，要构建对方架构。一次构建两种架构并直接推进仓库：

```bash
bash scripts/build-images.sh "$TAG" --registry <REG> --platform linux/amd64,linux/arm64 --push
```

- 在 Linux 构建机上先装一次跨架构模拟：`docker run --privileged --rm tonistiigi/binfmt --install all`（Mac 的 Docker Desktop / OrbStack 已自带）
- `--push` 有两道闸：必须给 `--registry`；必须两种架构，或显式加 `--single-arch` 认下「只推本机架构」。
  单架构推上去，推送这边一切正常，**只有另一种架构的节点会拉不到**（`no matching manifest`）。
- 推完核对：`docker buildx imagetools inspect <REG>/gaussdb-agent-runtime:$TAG`，应看到 `linux/amd64` 与 `linux/arm64` 两行。

用这种方式推了的，跳过第 4 章的 `docker tag` / `docker push`。

### 3.5 取标准知识库镜像（不从源码构建）

第 5 个镜像 `gaussdb-agent-kbstd` 是标准知识库（GaussDB 官方文档集）。它的内容是官方文档的派生，**只发布在镜像仓与离线包里、不进代码仓库**，
所以不能也不需要从源码构建。二选一取到本机，标签与其余四个一致：

```bash
docker pull ghcr.io/sqlrush/gaussdb-agent-kbstd:$TAG && docker tag ghcr.io/sqlrush/gaussdb-agent-kbstd:$TAG gaussdb-agent-kbstd:$TAG
# 或:从离线包载入(包里五个镜像都有;标签带架构后缀时先去掉,见《部署手册》§1.2–1.3)
```

> `docker images | grep kbstd` 有一行。部署后由 Job 自动装进知识库（5.7）。

---

## 4. 推进镜像仓库

`REG` 是镜像仓库前缀，例如 `registry.example.com/gaussdb-agent`。先登录（有权限要求的仓库）：

```bash
REG=<镜像仓库前缀>
docker login ${REG%%/*}
```

> `Login Succeeded`。

```bash
for img in runtime kb-import gateway frontend kbstd; do
  docker tag gaussdb-agent-$img:$TAG $REG/gaussdb-agent-$img:$TAG
  docker push $REG/gaussdb-agent-$img:$TAG
done
```

> 每个 push 最后一行是 `...: digest: sha256:... size: ...`。`denied` 是没有推送权限，找容器平台管理员。

---

## 5. 部署到 Kubernetes

下面的命令在跳板机的仓库目录里执行（跳板机与构建机可以是同一台）。跳板机上也要定义同样的 `REG` 与 `TAG`。

```mermaid
flowchart TD
    S0["5.0 中间件导入白名单<br/>(中间件团队)"] --> S1["5.1 清单指向镜像仓库"]
    S1 --> S2["5.2 接上存储 (NAS)"]
    S2 --> S3["5.3 创建全部基础资源<br/>kubectl apply -k k8s/base"]
    S3 --> S4["5.4 三个 Secret"]
    S4 --> S5["5.5 填 agent-config"]
    S5 --> S6["5.6 出网策略按实际地址改"]
    S6 --> S7["5.7 知识库索引<br/>(不用初始化)"]
    S7 --> S8["5.8 启动网关、前端"]
    S8 --> S9["5.9 开通工号<br/>建令牌 + runtime-doctor --fix"]
    S9 --> S10["5.10 命令行验收"]
    S10 --> S11["5.11 浏览器里用起来"]
```

### 5.0 中间件导入诊断脚本白名单（中间件团队做）

用户容器里的技能只能按名字调用中间件里预先登记的脚本（白名单）。**白名单里没有的脚本，对应的功能用不了**
（报「未注册脚本」或「脚本不存在」）。这一步由中间件团队在部署前做，我方提供 DML（全部是只读查询，只访问系统视图与系统表）：

| 情况 | 用哪份 | 条数 |
|---|---|---|
| 第一次部署 | `scripts.sql`（全量） | 137 |
| 已导入过 128 条（v1.4.3 ~ v1.4.6） | `scripts-delta-20261105.sql`（带每条说明与导入后自检） | 新增 9、更新 2 |
| 已导入过 126 条（v1.4.0 ~ v1.4.2） | 先 `scripts-delta-20261030.sql`，再 `scripts-delta-20261105.sql` | 新增 2，再按上一行 |
| 已导入过 123 条（v1.3.1 / v1.3.2） | 先 `scripts-delta-20261024.sql`，再 `scripts-delta-20261026.sql`，再按上一行的两份 | 更新 12，新增 3，再按上一行 |
| 已导入过 118 条（v1.3.0） | 先 `scripts-delta-20261022.sql`，再上一行的四份 | 新增 5，再按上一行 |
| 已导入过更早的 105 条 | 最先 `scripts-delta-20261018.sql`，再依次导上面五份 | 新增 13、更新 9，再按上两行 |

文件在本仓库的 `docs/delivery/`（交付给行内的诊断技能包里在 `whitelist/`），逐条的用途、参数与 SQL 全文见同目录
`白名单脚本清单.md`。导完用增量文件末尾的自检 SQL 核对：总数应为 137。

### 5.1 清单指向镜像仓库

```bash
sed -i "s#image: gaussdb-agent-gateway:.*#image: $REG/gaussdb-agent-gateway:$TAG#" k8s/base/gateway.yaml
sed -i "s#image: gaussdb-agent-frontend:.*#image: $REG/gaussdb-agent-frontend:$TAG#" k8s/base/frontend.yaml
sed -i "s#image: gaussdb-agent-kbstd:.*#image: $REG/gaussdb-agent-kbstd:$TAG#" k8s/base/kbstd-install.yaml
sed -i "s#image: gaussdb-agent-kb-import:.*#image: $REG/gaussdb-agent-kb-import:$TAG#" k8s/base/kbstd-install.yaml
sed -i "s#  USER_IMAGE_REGISTRY: .*#  USER_IMAGE_REGISTRY: \"$REG\"#; s#  USER_IMAGE_TAG: .*#  USER_IMAGE_TAG: \"$TAG\"#" k8s/base/configmap-gateway.yaml
```

> 在 Mac 上 `sed -i` 要写成 `sed -i ''`，其余不变。

```bash
grep -rn "image:\|USER_IMAGE_REGISTRY:\|USER_IMAGE_TAG:" k8s/base/ | grep -v ":\s*#"
```

> 6 行，地址都以你的 `REG` 开头、版本都是你的 `TAG`：
> ```
> k8s/base/configmap-gateway.yaml:12:  USER_IMAGE_TAG: "agent-v1.5.3-oc1.18.27"
> k8s/base/configmap-gateway.yaml:16:  USER_IMAGE_REGISTRY: "<REG>"
> k8s/base/frontend.yaml:25:          image: <REG>/gaussdb-agent-frontend:agent-v1.5.3-oc1.18.27
> k8s/base/gateway.yaml:91:          image: <REG>/gaussdb-agent-gateway:agent-v1.5.3-oc1.18.27
> k8s/base/kbstd-install.yaml:29:          image: <REG>/gaussdb-agent-kbstd:agent-v1.5.3-oc1.18.27
> k8s/base/kbstd-install.yaml:41:          image: <REG>/gaussdb-agent-kb-import:agent-v1.5.3-oc1.18.27
> ```
> 后两行是标准知识库安装 Job（第 5.7 节知识库）：部署后它把官方文档集装进 NAS，用到标准知识库镜像与导入镜像。

> **`USER_IMAGE_REGISTRY` 必须填。** 用户容器（runtime、kb-import）不在任何清单文件里：
> 网关在用户登录时按 `<USER_IMAGE_REGISTRY>/gaussdb-agent-runtime:<USER_IMAGE_TAG>` 现建。
> 留空的话镜像名不带仓库前缀，节点会去 Docker Hub 拉，用户一登录就卡在 `ImagePullBackOff`，浏览器等 2 分钟后 504。

### 5.2 接上存储（NAS）

所有用户容器共用一个 RWX 卷，各自挂其中一个子目录。二选一：

- **集群已有 RWX 的 StorageClass**：把 `k8s/base/nas-pvc.yaml` 里的 `storageClassName: nas` 改成那个类名，跳过本节其余部分。
- **用 NFS**：

```bash
cp k8s/base/nas-pv-nfs.example.yaml k8s/base/nas-pv-nfs.yaml
vi k8s/base/nas-pv-nfs.yaml      # 只改最后两行:server 与 path
kubectl apply -f k8s/base/nas-pv-nfs.yaml
```

> `persistentvolume/nas-nfs created`

**存储管理员要先在导出目录下建好 `users/`、`admins/`、`kb/` 三个目录，属主 uid/gid 都是 1000**：

```bash
# 在 NFS 服务器上(或任何挂了这个导出目录、有 root 权限的机器上)
mkdir -p <导出路径>/users <导出路径>/admins <导出路径>/kb
chown -R 1000:1000 <导出路径>
```

> 单节点测试集群没有 NFS 时用节点本机目录，见附录 A.3。

### 5.3 创建全部基础资源

```bash
kubectl apply -k k8s/base
```

> ```
> namespace/gaussdb-agent created
> serviceaccount/gateway created
> role.rbac.authorization.k8s.io/gateway created
> rolebinding.rbac.authorization.k8s.io/gateway created
> configmap/agent-config created
> configmap/agent-roles created
> configmap/gateway-config created
> service/frontend created
> service/gateway created
> persistentvolumeclaim/nas created
> deployment.apps/frontend created
> deployment.apps/gateway created
> job.batch/kbstd-install-v1-4-7 created
> networkpolicy.networking.k8s.io/frontend-policy created
> networkpolicy.networking.k8s.io/gateway-policy created
> networkpolicy.networking.k8s.io/kb-import-policy created
> networkpolicy.networking.k8s.io/kbstd-policy created
> networkpolicy.networking.k8s.io/runtime-policy created
> ```

```bash
kubectl -n gaussdb-agent get pvc
kubectl -n gaussdb-agent get pods
```

> PVC `nas` 是 `Bound`。Pod 里：
> - `frontend` 是 `Running`：说明节点能从你的镜像仓库拉镜像；
> - `gateway` 是 `CrashLoopBackOff`：**正常**。还没建信任凭据，网关按设计拒绝启动，5.8 之后就好；
> - `kbstd-install-v1-4-7-…` 是 `Completed`：官方文档集已装进知识库（5.7）。NAS 还没接好时它会失败重试，接好后自己就过。
>
> `frontend` 卡在 `ImagePullBackOff`：节点拉不到镜像，检查 `REG` 与仓库权限（私有仓库要配 `imagePullSecrets` 或节点级凭据）。

### 5.4 三个 Secret

**模型服务的 key**（输入时不显示）：

```bash
read -rs -p "模型服务 key: " V && echo
kubectl -n gaussdb-agent create secret generic model-api --from-literal=MODEL_API_KEY="$V"
unset V
```

**网关信任凭据**：上游 SSO 转发请求时带上它，网关才认请求头里的工号（5.11 的模拟 SSO 也要用）：

```bash
TRUST=$(openssl rand -hex 24)
kubectl -n gaussdb-agent create secret generic gateway-auth --from-literal=TRUST_SECRET="$TRUST" --from-literal=ADMIN_TOKEN="$(openssl rand -hex 24)"
printf '%s' "$TRUST" > ~/.agent-trust && chmod 600 ~/.agent-trust
```

**中间件签名私钥**（中间件要求 SM2 签名时才需要；不要求就跳过）：

```bash
read -rs -p "SM2 私钥: " V && echo
kubectl -n gaussdb-agent create secret generic grmp-sm2 --from-literal=GRMP_SM2_PRIVATE_KEY="$V"
unset V
```

```bash
kubectl -n gaussdb-agent get secret
```

> `gateway-auth`、`model-api` 两行（建了签名私钥的再多一行 `grmp-sm2`）。

### 5.5 填 agent-config

```bash
kubectl -n gaussdb-agent patch configmap agent-config --type merge -p '{"data":{
  "GRMP_API_HOST":"<中间件IP>","GRMP_API_PORT":"<中间件端口>","GRMP_PATH_PREFIX":"<中间件接口路径前缀>",
  "MODEL_BASE_URL":"<模型服务地址,如 https://api.deepseek.com/v1>","MODEL_ID":"<模型名>","MODEL_PROVIDER_NAME":"<随便起个名字,如 deepseek>"}}'
```

中间件要求签名时再填 Appkey（签名的其余旋钮见《参数手册》，默认值是国标 / Java 常见形态）：

```bash
kubectl -n gaussdb-agent patch configmap agent-config --type merge -p '{"data":{"GRMP_APPKEY":"<中间件给的 Appkey>"}}'
```

```bash
kubectl -n gaussdb-agent get configmap agent-config -o go-template='{{range $k,$v := .data}}{{$k}}={{$v}}{{"\n"}}{{end}}' | grep -E "^(GRMP_API|GRMP_APPKEY|MODEL_)"
```

> 验证时的值（中间件是测试用的模拟服务）：
> ```
> GRMP_API_HOST=0.250.250.254
> GRMP_API_PORT=8781
> GRMP_APPKEY=gaussdb-agent
> MODEL_BASE_URL=https://api.deepseek.com/v1
> MODEL_ID=deepseek-flash
> MODEL_PROVIDER_NAME=deepseek
> ```

**中间件地址不能写 `localhost` / `127.0.0.1`**：那在容器里指容器自己。

### 5.6 出网策略按实际地址改

清单里的出网地址段是占位值（`10.0.0.0/8`）。不改的话，要么用户容器连不上模型和中间件，要么网关连不上 k8s API。要改 4 处：

| 策略 | 第几条出站规则（从 0 数） | 放行什么 | 填什么 |
|---|---|---|---|
| `gateway-policy` | 3 | 网关 → k8s API | **apiserver 的真实地址**（下面查） |
| `runtime-policy` | 1 | 用户容器 → 中间件 | 中间件 IP/32 与端口 |
| `runtime-policy` | 2 | 用户容器 → 模型服务 | 模型服务的地址段；模型在公网时只能 `0.0.0.0/0`（端口 443/80） |
| `kb-import-policy` | 1 | 知识库导入容器 → 模型服务 | 同上 |

**查 apiserver 的真实地址**（NetworkPolicy 按真实端点判，不按 Service 的 ClusterIP）：

```bash
kubectl -n default get endpoints kubernetes
```

> ```
> NAME         ENDPOINTS            AGE
> kubernetes   192.168.155.3:6443   13m
> ```
> 例子里是 `192.168.155.3`；多 master 的集群会有多个，每个都要放行。

```bash
APISERVER=<上面查到的 IP>
GRMP_IP=<中间件IP>; GRMP_PORT=<中间件端口>
MODEL_CIDR=0.0.0.0/0          # 模型服务在内网且地址固定时,换成它的地址段
kubectl -n gaussdb-agent patch networkpolicy gateway-policy --type json -p "[{\"op\":\"add\",\"path\":\"/spec/egress/3/to/-\",\"value\":{\"ipBlock\":{\"cidr\":\"$APISERVER/32\"}}}]"
kubectl -n gaussdb-agent patch networkpolicy runtime-policy --type json -p "[{\"op\":\"replace\",\"path\":\"/spec/egress/1\",\"value\":{\"to\":[{\"ipBlock\":{\"cidr\":\"$GRMP_IP/32\"}}],\"ports\":[{\"port\":$GRMP_PORT,\"protocol\":\"TCP\"}]}},{\"op\":\"replace\",\"path\":\"/spec/egress/2/to\",\"value\":[{\"ipBlock\":{\"cidr\":\"$MODEL_CIDR\"}}]}]"
kubectl -n gaussdb-agent patch networkpolicy kb-import-policy --type json -p "[{\"op\":\"replace\",\"path\":\"/spec/egress/1/to\",\"value\":[{\"ipBlock\":{\"cidr\":\"$MODEL_CIDR\"}}]}]"
```

> 三行 `... patched`。核对：

```bash
for p in gateway-policy runtime-policy kb-import-policy; do echo "--- $p"; kubectl -n gaussdb-agent get networkpolicy $p -o jsonpath='{range .spec.egress[*]}{.to[*].ipBlock.cidr}{" "}{.to[*].podSelector.matchLabels}{" -> "}{.ports[*].port}{"\n"}{end}'; done
```

> ```
> --- gateway-policy
>  {"k8s-app":"kube-dns"} -> 53 53
>  {"app":"gaussdb-agent"} -> 4096 4097
>  {"app":"gaussdb-frontend"} -> 8080
> 10.96.0.0/12 172.16.0.0/12 192.168.155.3/32  -> 443 6443
> --- runtime-policy
>  {"k8s-app":"kube-dns"} -> 53 53
> 0.250.250.254/32  -> 8781
> 0.0.0.0/0  -> 443 80
> --- kb-import-policy
>  {"k8s-app":"kube-dns"} -> 53 53
> 0.0.0.0/0  -> 443 80
> ```

> 公司出网要经 HTTPS 代理时，模型那条不用放开 `0.0.0.0/0`：开通工号后执行
> `bash k8s/tools/runtime-doctor.sh <工号> --fix --proxy <代理IP:端口>`，它给用户容器配上代理，并把模型那条改成只放行代理。

**验证策略确实在生效**（网络插件不支持 NetworkPolicy 时策略不报错、只是不生效）：起两个临时 Pod，一个带用户容器的标签（受 `runtime-policy` 约束），
一个不带，各访问一次中间件（放行了）和集群里的网关（用户容器的出站规则里没有它）：

```bash
for lbl in "app=gaussdb-agent,role=runtime,user=policytest" "app=policytest"; do
  kubectl -n gaussdb-agent run pt-$RANDOM --rm -i --restart=Never --labels="$lbl" --image=$REG/gaussdb-agent-runtime:$TAG --command -- \
    sh -c "sleep 8; echo \"[$lbl] 中间件 -> \$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://$GRMP_IP:$GRMP_PORT/)  网关 -> \$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://gateway/healthz)\"" 2>&1 | grep 中间件
done
```

> ```
> [app=gaussdb-agent,role=runtime,user=policytest] 中间件 -> 404  网关 -> 000
> [app=policytest] 中间件 -> 404  网关 -> 200
> ```
> - 中间件那一列是任何 HTTP 码都算通（`000` 才是不通：回头查 `GRMP_IP` / `GRMP_PORT` 与中间件的防火墙）；
> - **带用户容器标签的那个访问网关必须是 `000`**。两行都是 `200`，说明集群没在执行 NetworkPolicy，找容器平台确认网络插件。
> - `sleep 8` 不能省：新 Pod 的策略要过几秒才生效，抢在之前测会误以为没生效。

### 5.7 知识库（不用初始化；官方文档集自动装上）

**没有向量库要初始化。** 知识库的向量 + 词法索引是 NAS 上知识库目录里的一个文件 `kb/index/store.sqlite`
（SQLite + sqlite-vec，runtime 与 kb-import 两个镜像里都已装好），由导入容器里的 `kb.py index` 建：
管理员导入规范 / 工单时导入流程会自动跑；runtime 与固定池化的槽位只读打开它。还没建索引时检索直接走 `kb/` 下的文件（文件模式），
不影响使用。要手工建一次（例如知识库目录是从别处拷来的）：

```bash
kubectl -n gaussdb-agent exec deploy/kb-import-<管理员工号> -- sh -c '. /data/env.sh && python3 /opt/agent/config/opencode/skills/gaussdb-kb-import/scripts/kb.py index'
```

> 导入容器要在运行（管理员登录进导入容器一次即可）。embedding 服务另行配置，见《部署手册》阶段 5；不配不影响诊断功能。

**官方文档集随部署自动装上。** 5.3 的 `apply -k` 建了一次性 Job `kbstd-install-v1-4-7`：它从标准知识库镜像里取出包，
用导入镜像里的 `kb.py docset install` 装进 NAS 的 `kb/docsets/`（逐个文件核对 sha256，同版本跳过，不碰客户自己的知识），装完重建 L0 / L1。核对：

```bash
kubectl -n gaussdb-agent logs job/kbstd-install-v1-4-7
```

> ```
> [installed] G25132C 已装上:云数据库 GaussDB 25.1.32 集中式版产品文档(for 华为云Stack 8.5.1) 01 · 6023 条 · 版本 ebd6bf6b5f294395-…
> 分层        : L0 / L1 已按装好的文档集重建
> ```
> 再 apply 一次会是 `[skipped] G25132C 已是这个版本…,不动`。之后在对话里问一个参数的默认值，回答里能引用 `PD-G25132C-…` 并按产品文档上青色。

### 5.8 启动网关、前端

```bash
kubectl -n gaussdb-agent rollout restart deploy/gateway
kubectl -n gaussdb-agent rollout status deploy/gateway --timeout=180s
kubectl -n gaussdb-agent logs -l app=agent-gateway --tail=2
```

> 最后出现：
> ```
> 网关就绪:0.0.0.0:8080 · 命名空间 gaussdb-agent · 用户镜像标签 agent-v1.5.3-oc1.18.27 · 回收 scale(闲置 1800 秒)· 信任来源 共享密钥 · 控制 API 已启用
> ```

```bash
kubectl -n gaussdb-agent rollout status deploy/frontend --timeout=180s
kubectl -n gaussdb-agent get pods
```

> ```
> NAME                       READY   STATUS    RESTARTS   AGE
> frontend-8c96c467c-t52m6   1/1     Running   0          85s
> gateway-84bd57466-nprjf    1/1     Running   0          3s
> gateway-84bd57466-qfl5h    1/1     Running   0          1s
> ```

### 5.9 开通工号

开通一个工号 = 两步：建这个工号的中间件令牌，再用诊断脚本把它的个人目录建好并做一遍检查。

```bash
STAFF=<工号>          # 例如 u0001,小写
read -rs -p "$STAFF 的中间件令牌: " V && echo
kubectl -n gaussdb-agent create secret generic grmp-token-$STAFF --from-literal=GRMP_AUTH_TOKEN="$V"
unset V
bash k8s/tools/runtime-doctor.sh $STAFF --fix
```

> ```
> 诊断工号 u0002(命名空间 gaussdb-agent)…… 约 30 秒到 1 分钟
> == ① 配置与开通 ==
>   [正常] 模型地址 https://api.deepseek.com/v1,模型 deepseek-flash
>   [正常] 模型 key 已配置(Secret model-api,不打印内容)
>   [正常] 中间件地址 0.250.250.254:8781
>   [正常] 已开通(有 Secret grmp-token-u0002)
> == ② NAS 个人目录 ==
>   [问题] users/u0002 还没建:首次登录时会被自动建成 root 属主,容器写不进
> == ③ 用户容器 runtime-u0002 ==
>          还没有 runtime-u0002:这个工号还没登录过,首次登录时网关会创建
> == ④ 出网实测(从集群里连) ==
>   [正常] 模型服务 https://api.deepseek.com/v1:用户容器能连上,通(HTTP 401)
>          401 是因为探测没带 key,网络是通的
>   [正常] 中间件 http://0.250.250.254:8781:用户容器能连上,通(HTTP 404)
> ...
> == 修复 ==
>   [已改] 个人目录 users/u0002 已建好,属主 1000
>   runtime-u0002 当前没有运行,下次登录时会按新的配置与目录拉起
> ...
> ================ 诊断意见 ================
> 全部正常。对话页刷新后重试即可。
> ```

> **为什么要先建个人目录：** 首次登录时，如果 `users/<工号>` 还不存在，kubelet 会以 root 身份自动创建它，
> 而用户容器以 uid 1000 运行，写不进，容器一启动就退出（日志 `mkdir: cannot create directory '/nas/me/gdaa': Permission denied`）。
> 先登录了也没关系，执行同一条 `runtime-doctor.sh <工号> --fix` 会修好目录并重启容器。
>
> ④ 是从集群里实际连一次模型与中间件。哪一项不通，「诊断意见」里会写原因和可以执行的修复命令。

### 5.10 命令行验收

把网关接到本机端口（这个窗口保持打开）：

```bash
kubectl -n gaussdb-agent port-forward svc/gateway 18089:80
```

> `Forwarding from 127.0.0.1:18089 -> 8080`。提示 `address already in use` 就换一个端口，下面跟着换。

另开一个终端（回到仓库目录，重新定义 `STAFF`）：

```bash
STAFF=<工号>; TRUST=$(cat ~/.agent-trust)
curl -s http://127.0.0.1:18089/healthz; echo
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Agent-User: $STAFF" http://127.0.0.1:18089/session
curl -s -m 150 -o /dev/null -w "%{http_code}\n" -H "X-Agent-Trust: $TRUST" -H "X-Agent-User: $STAFF" http://127.0.0.1:18089/global/health
kubectl -n gaussdb-agent get deploy runtime-$STAFF -o wide
```

> ```
> {"ok": true, "version": "gaussdb-agent-gateway"}
> 403
> 200
> NAME            READY   UP-TO-DATE   AVAILABLE   AGE   CONTAINERS   IMAGES
> runtime-u0002   1/1     1            1           8s    opencode     <REG>/gaussdb-agent-runtime:agent-v1.5.3-oc1.18.27
> ```
> - `403`：不带信任凭据的请求被拒，**必须是 403**；
> - `200`：首次登录网关为这个工号新建容器，实测 8 秒；
> - `IMAGES` 列带着你的 `REG` 前缀。
>
> 第三条挂着超过 30 秒没返回，说明容器起不来（网关会等满 2 分钟才回 504）。不用干等，另开终端执行
> `bash k8s/tools/runtime-doctor.sh <工号>`，它会说明原因并给出修复命令。

### 5.11 浏览器里用起来

生产环境由贵方 SSO 在认完人之后把工号头和信任凭据注入请求（见《接入手册》）。SSO 还没接时，
用仓库里的「模拟 SSO」在本机站在 SSO 的位置上（需要 Python 3.8+，只用标准库）。保持 5.10 的 port-forward 窗口开着，再开一个终端：

```bash
python3 k8s/tools/sso-sim.py 18090 18089 <工号> ~/.agent-trust
```

> `模拟 SSO:http://127.0.0.1:18090 → 网关 127.0.0.1:18089,工号 u0001(头名 X-Agent-User)`。这个窗口也保持打开。

浏览器（必须在同一台机器上）打开：

| 看什么 | 地址 |
|---|---|
| 对话页 | `http://127.0.0.1:18090/L2RhdGEvc3RhdGUvd29ya3NwYWNl` |
| 四个大盘 | `http://127.0.0.1:18090/dash/health`（另有 `/dash/topsql`、`/dash/wdr`、`/dash/kb`） |

在对话页新建会话，输入：

```
登录 <数据库IP> 的 <库名> 库,然后做一次健康检查。不要向我提问,遇到选择按默认做。完成后用三五句话总结结论。
```

模型会经中间件登录数据库，跑健康检查的各个子项，最后给出报告与结论（实测约 1 分钟）：

![对话页：模型完成健康检查后的结论](assets/build-deploy/chat.png)

报告同时存档，打开健康检查大盘就能看到（左侧「实例」下拉框切换数据库；每条发现右侧的「深挖」会带着上下文回到对话页）：

![健康检查大盘](assets/build-deploy/dash-health.png)

**到这里，从源码到运行的全过程完成。** 模拟 SSO 的更多用法（多个工号、知识库管理员角色、安全须知）见《现场测试-模拟SSO》。
测完在两个窗口按 Ctrl-C，并删掉 `~/.agent-trust`。

---

## 6. 升级

改了代码（或拉了新版本）之后。**先看《版本说明》「升级前先看」**：要导白名单增量的版本（如 v1.3.0），先让中间件团队导入（5.0），再换镜像。

```bash
git pull
TAG=$(git describe --tags --always)-oc1.18.27
bash scripts/build-images.sh "$TAG"          # 或 3.4 的多架构 --push 方式;标准知识库镜像按 3.5 取
for img in runtime kb-import gateway frontend kbstd; do
  docker tag gaussdb-agent-$img:$TAG $REG/gaussdb-agent-$img:$TAG && docker push $REG/gaussdb-agent-$img:$TAG
done
kubectl -n gaussdb-agent set image deploy/gateway gateway=$REG/gaussdb-agent-gateway:$TAG
kubectl -n gaussdb-agent set image deploy/frontend nginx=$REG/gaussdb-agent-frontend:$TAG
kubectl -n gaussdb-agent patch configmap gateway-config --type merge -p "{\"data\":{\"USER_IMAGE_TAG\":\"$TAG\"}}"
kubectl -n gaussdb-agent rollout restart deploy/gateway
kubectl apply -f k8s/base/kbstd-install.yaml   # 标准知识库:先按 5.1 改好它的镜像地址;新版本的 Job 是新对象,同版本跳过
```

> `USER_IMAGE_TAG` 是用户容器的升级开关：改了之后，**之后登录**的人用新版本，已经在用的人的容器不动。
> NAS 上的数据在小版本间兼容，回滚把上面的 `TAG` 换回旧值即可。
> 只 apply 安装 Job 这一个文件，不要整个 `apply -k`：那会把两个 ConfigMap 恢复成清单里的占位值。
> 从带向量库（`vectordb`）的旧版本（v1.3.1 及更早）升级时多一步一次性操作（建索引、删掉 vectordb 残留对象），见《部署手册》§10.6。

---

## 7. 常见问题

照本手册部署时可能遇到的问题。前两个是写本手册时发现的，代码与脚本已修复；表里同时写了老版本上的处理办法。

| 现象 | 原因 | 处理 |
|---|---|---|
| 用户容器反复重启，日志 `mkdir: cannot create directory '/nas/me/...': Permission denied` | 个人目录被 kubelet 以 root 建成，容器的 uid 1000 写不进 | `bash k8s/tools/runtime-doctor.sh <工号> --fix`（5.9） |
| 登录后等 2 分钟 504；`kubectl get pod` 看到用户容器 `ImagePullBackOff`，镜像名不带仓库前缀 | `gateway-config` 的 `USER_IMAGE_REGISTRY` 没填 | 填上（5.1），`kubectl -n gaussdb-agent rollout restart deploy/gateway`，删掉那个用户的 deploy 后重新登录 |
| `port-forward` 报 `address already in use` | 本机端口被别的程序占着 | 换一个端口，后面的地址跟着换 |
| 网关 `CrashLoopBackOff`，日志 `配置错误,网关拒绝启动:...` | 缺配置或 Secret（网关缺信任配置时刻意拒绝启动） | 按日志说的补；5.4 之前出现属正常 |
| 网关日志 `连不上 k8s API` | `gateway-policy` 没放行 apiserver 的真实地址 | 5.6 第一条 |
| `frontend` 或网关 `ImagePullBackOff` | 节点拉不到镜像：地址错、没推、没权限、架构不对 | `kubectl -n gaussdb-agent describe pod <名字> \| tail` 看原因；架构不对用 3.4 推两种架构 |
| 对话页报 `Cannot connect to API`，或登录数据库连不上中间件 | 出网不通、地址填错 | `bash k8s/tools/runtime-doctor.sh <工号>`，按「诊断意见」执行 |

更多现象与排查见《部署手册》附录 A。

---

## 附录 A：用 kind 起一个单机测试集群（本手册的验证环境）

需要一台装了 Docker 的机器（Linux 或 macOS），以及 [kind](https://kind.sigs.k8s.io/) 与 `kubectl`。
集群跑在 Docker 容器里，和机器上原有的 Kubernetes 互不影响。

### A.1 本地镜像仓库 + 集群

```bash
docker run -d --restart=always -p 127.0.0.1:5001:5000 --network bridge --name gaussdb-e2e-registry registry:3

cat <<'EOF' | kind create cluster --name gaussdb-e2e --kubeconfig ~/gaussdb-e2e.kubeconfig --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
containerdConfigPatches:
- |-
  [plugins."io.containerd.grpc.v1.cri".registry]
    config_path = "/etc/containerd/certs.d"
EOF

for node in $(kind get nodes --name gaussdb-e2e); do
  docker exec "$node" mkdir -p /etc/containerd/certs.d/localhost:5001
  printf '[host."http://gaussdb-e2e-registry:5000"]\n' | docker exec -i "$node" cp /dev/stdin /etc/containerd/certs.d/localhost:5001/hosts.toml
done
docker network connect kind gaussdb-e2e-registry

export KUBECONFIG=~/gaussdb-e2e.kubeconfig
kubectl get nodes
```

> ```
> NAME                        STATUS   ROLES           AGE   VERSION
> gaussdb-e2e-control-plane   Ready    control-plane   70s   v1.36.1
> ```

- `--kubeconfig` 让这个集群的凭据写进单独的文件，**不改动你原来的 kubectl 当前上下文**；之后在这个终端里 `export KUBECONFIG=...` 即可。
- 镜像仓库在本机 `localhost:5001`，集群节点经 `hosts.toml` 访问它。第 4 章用 `REG=localhost:5001`，不需要 `docker login`。
- kind 自带的网络插件执行 NetworkPolicy（5.6 的验证方法在上面实测有效）。

### A.2 apiserver 地址

5.6 里查到的就是 kind 节点的 IP（例子里的 `192.168.155.3`），照常放行。

### A.3 存储：节点本机目录

单节点没有 NFS，用节点上的一个目录。先在节点里建好三个子目录、属主改成 1000：

```bash
docker exec gaussdb-e2e-control-plane sh -c 'mkdir -p /srv/gaussdb-agent-nas/users /srv/gaussdb-agent-nas/admins /srv/gaussdb-agent-nas/kb && chown -R 1000:1000 /srv/gaussdb-agent-nas && ls -ln /srv/gaussdb-agent-nas'
```

再建一个指向它的 PV（代替 5.2 的 NFS PV）：

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nas-hostpath
spec:
  capacity:
    storage: 500Gi
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: nas
  hostPath:
    path: /srv/gaussdb-agent-nas
    type: Directory
EOF
```

> `persistentvolume/nas-hostpath created`。**只适用于单节点测试集群**：多节点时各节点的目录互不相通。

### A.4 不用镜像仓库

kind 也可以把镜像直接载到节点上：`kind load docker-image <镜像> --name gaussdb-e2e`，四个镜像各执行一次。
这时清单里的镜像名不带仓库前缀、`USER_IMAGE_REGISTRY` 留空（5.1 的 sed 里 `REG` 部分都不做）。
**正式环境不要只靠节点本地镜像**：用户全部闲置回收后，kubelet 可能把没人引用的镜像清掉，之后用户登录就拉不到了。

---

## 附录 B：清理

kind 测试集群整个删掉：

```bash
kind delete cluster --name gaussdb-e2e
docker rm -f gaussdb-e2e-registry
rm -f ~/gaussdb-e2e.kubeconfig
```

在正式集群上删掉整套部署（NAS 上的数据保留）：

```bash
kubectl delete namespace gaussdb-agent
# PV 是 Retain,删命名空间后变成 Released、仍记着旧的 PVC,下次部署会绑不上。重新部署前解除旧绑定:
kubectl patch pv nas-nfs --type json -p '[{"op":"remove","path":"/spec/claimRef"}]'
```

---

## 附录 C：验证记录

> 2026-10-23 起第 5 个镜像（openGauss 向量库）与它的初始化步骤下线，知识库索引改为 NAS 上的 `kb/index/store.sqlite`。
> 下表是当时的原样记录（2026-10-15 那次含第 5 个镜像）；5.7 的手工建索引命令是改版后新加的，尚未在本附录的环境里从空集群重跑。

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-15 |
| 代码 | `main` 292a4b0（构建脚本与第 5 个镜像 95c6b96 起即可用） |
| 构建机 | macOS（Apple Silicon），Docker 29.4，buildx 0.33，新建的 `docker-container` 构建器（无缓存） |
| 4 个应用镜像 | arm64 从零构建 96 秒；amd64 交叉构建 77 秒通过 |
| 第 5 个镜像 | arm64 构建 70 秒，起库 18 秒就绪；amd64 构建 79 秒，起库 21 秒就绪；两种架构都验了 `vector` 类型、`<=>` 算子、`GS_DB` 建库 |
| 集群 | kind v0.32.0，单节点，Kubernetes v1.36.1，containerd 2.3.1，kindnet（执行 NetworkPolicy） |
| 镜像仓库 | `registry:3`，本机 `localhost:5001` |
| 模型 / 中间件 | DeepSeek（`deepseek-flash`）/ 中间件模拟服务（开 SM2 签名） |
| 结果 | 5.3–5.11 全部按本手册的输出通过：网关 2 副本就绪，首次登录 8 秒，`runtime-doctor` 全部正常，对话页完成一次健康检查，大盘显示报告 |
| 2026-10-19 agent-v1.3.0 | 在发布标签上重跑第 3 章：4 个应用镜像本机构建 + amd64 交叉构建，3.3 的输出即本次结果；两种架构推 GHCR 并合成多架构清单；离线包载回后两种架构镜像冒烟各 16/16；在本机 OrbStack 集群上按第 6 章换镜像升级，浏览器里完成对话与四个大盘。**第 5 章（从空集群部署）未重跑**：v1.2.0 以来 K8s 清单除镜像标签外没有改动 |
| 2026-10-22 agent-v1.3.1 | 在发布标签上按第 3 章本机构建 + amd64 交叉构建；两种架构推 GHCR 并合成多架构清单（推送改用 `gh auth refresh -s write:packages` 授权后的 gh 凭据，原 `~/.ghcr_token` 已过期）；离线包两种架构载回成功；在本机 OrbStack 集群上按第 6 章换镜像升级，u9500 对话级冒烟 4/4（时段锁回溯、时段体检的锁维度、历史真实执行计划、WDR 附录真实计划）。**单机镜像冒烟未跑**：8779 验收环境未起（重建它会覆盖测试集群中间件的白名单库），以集群内对话级冒烟代替。**第 5 章（从空集群部署）未重跑**：清单除镜像标签外没有改动 |
| 过程中发现并已修复 | 网关给用户建容器时不带镜像仓库前缀（新增 `USER_IMAGE_REGISTRY`）；三份旧手册里向量库镜像的 sed 写错；`init-vectordb.sh` 写死了 kubectl 上下文；向量库镜像缺 `find` 与 `en_US.utf8` |
