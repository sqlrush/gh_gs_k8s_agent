# k8s 部署手册（给平台）

对象：`gaussdb-agent-runtime`（每用户一个 Pod）、`gaussdb-agent-kb-import`（知识库管理员）。清单在 `k8s/`，环境契约在 `docs/env-contract.md`，设计在 `docs/specs/2026-09-15-k8s-containerization-design.md`。

## 1. 前提

| 项 | 要求 |
|---|---|
| 镜像 | 把 `gaussdb-agent-runtime`、`gaussdb-agent-kb-import`（标签 `agent-v0.4.1-oc1.18.27`，x86_64 / aarch64）推到内网镜像仓库 |
| NAS | 一个 RWX 卷，NFSv4.1；PV `mountOptions: [nfsvers=4.1, hard, local_lock=all]`；导出目录对 uid 1000 可写；目录结构 `users/<工号>/`、`admins/<工号>/`、`kb/` 由平台建好（uid 1000） |
| StorageClass | `nas`（或静态 PV 带 `storageClassName: nas`），能满足 `k8s/base/nas-pvc.yaml` 的 claim |
| CNI | 支持 NetworkPolicy（Calico / Cilium 等）。不支持时策略不生效，隔离只剩 Pod 与 subPath |
| 中间件 | GRMP 按人签发令牌；库的隔离靠它按人授权（spec §6.2 前提） |

## 2. 一次性

```bash
kubectl apply -k k8s/base
kubectl -n gaussdb-agent create secret generic model-api --from-literal=MODEL_API_KEY='<模型服务密钥>'
kubectl -n gaussdb-agent create secret generic grmp-sm2 --from-file=GRMP_SM2_PRIVATE_KEY=<客户签发的 SM2 私钥文件>   # 中间件开了签名校验才需要
kubectl -n gaussdb-agent edit configmap agent-config      # GRMP_API_HOST/PORT、GRMP_APPKEY 与 GRMP_SIGN_*、MODEL_BASE_URL、MODEL_ID
kubectl -n gaussdb-agent edit configmap agent-roles       # 没有 AD 组时:知识库管理员工号列表
kubectl -n gaussdb-agent edit networkpolicy runtime-policy kb-import-policy   # 把占位网段改成中间件与模型服务的实际地址段
```

`agent-roles` 是分组的 mock。SSO 能给组时，网关按组判定，这个 ConfigMap 不需要。

## 3. 按工号（网关做的事）

网关在用户登录后要做的四件事，等价于：

```bash
python3 scripts/k8s/provision.py <工号> --auto-role --image-tag agent-v0.4.1-oc1.18.27 --pull-policy Always \
    --token-env-file <含 GRMP_AUTH_TOKEN= 的文件>
```

1. `kubectl apply` Secret `grmp-token-<工号>`（本人 GRMP 令牌）与 `pod-auth-<工号>`（随机 24 字节，Pod 的 HTTP 基本认证口令，网关代理时带上）。
2. 判定角色：工号在 `agent-roles` 的 `kb-admins` 里（或 AD 组）→ 除 runtime 外再建 kb-import。
3. 渲染 `k8s/templates/runtime.yaml`（与 `kb-import.yaml`）：`${USER_ID}`、`${IMAGE}`、`${IMAGE_PULL_POLICY}`、`${SUBPATH_ROOT}`（runtime 用 `users`，kb-import 用 `admins`），`kubectl apply`。
4. 等 Deployment `availableReplicas == 1`（冷启动约 10 秒），再把流量代理到 Service `runtime-<工号>:4096`，请求头带 `Authorization: Basic base64(opencode:<口令>)`。

**浏览器落地页**：opencode serve 自带 Web 界面。把用户带到项目路由 `/<base64url("/nas/me/workspace")>`（即 `/L25hcy9tZS93b3Jrc3BhY2U`），打开的就是「新建会话」页（输入框 + 模型选择器），该项目的历史会话在侧栏；某条会话的地址是 `/L25hcy9tZS93b3Jrc3BhY2U/session/<会话 id>`。不要落在 `/`——那一页的「项目」列表是浏览器本地记的，新浏览器为空，用户得手动「添加项目」。entrypoint 首次启动会把 `/nas/me/workspace` 初始化成 git 仓库，opencode 以此识别项目。

**文件上传**（网关的第五件事）：网关提供上传入口，直接写 NAS，Pod 不参与——普通用户 → `users/<工号>/workspace/uploads/<文件名>`（Pod 内 `/nas/me/workspace/uploads/`）；知识库管理员另有目标 `kb/inbox/uploads/<文件名>`（kb-import Pod 内 `/nas/kb/inbox/uploads/`）。要求：以 uid 1000 写入；文件名白名单（字母数字 `._-` 与中文）；单文件上限平台定（建议 50 MB）；同名先备份再覆盖；上传成功后把 Pod 内路径回给用户，用户在对话里引用该路径。界面自带的「附件」不落 NAS，只适合短文本。

**CLI 接入**：用户本机装同版本 opencode，`OPENCODE_SERVER_PASSWORD=<口令> opencode attach http://<网关给的地址> --dir /nas/me/workspace`（口令也可 `--password`；`--dir` 必须是 Pod 内的工作目录）。CLI 与 Web 共用 NAS 上同一个 `opencode.db`，两边建的会话互相可见；`attach -c` 续接上一条会话。网关对 CLI 的要求与 Web 相同：代理到 4096 并处理基本认证（已验证，见阶段 3 记录的追加）。

回收：`provision.py <工号> --delete` —— 删 Deployment / Service / Secret，**不删 PVC 与 NAS 目录**。

模板里已经定死的（网关不用管）：`strategy: Recreate`、`terminationGracePeriodSeconds: 60`、uid 1000、`readOnlyRootFilesystem`、subPath 挂载、探针、`preStop: sleep 5`、资源配额。

## 4. 两种生命周期模式

| 模式 | 网关动作 |
|---|---|
| 对话结束回收 | 会话结束 → `provision.py <工号> --delete`（或只删 Deployment）。下次登录重新 provision，挂同一 `users/<工号>`，历史自动回来，**不需要恢复任何数据** |
| 保留 Pod | 不删；闲置超时 → `kubectl scale deploy runtime-<工号> --replicas=0`；重连 → `--replicas=1`。Deployment 的 annotation `gaussdb-agent/last-activity` 留给网关记最后活动时间 |

两种模式下都必须保证：同一工号同一时刻只有一个 runtime Pod（`Recreate` 策略 + 删除完成后再创建）。Pod 内的 `.owner` 标记是第二道保险。

## 5. 探针、停机、日志

- 就绪 / 存活：容器内 `curl -u "opencode:$OPENCODE_SERVER_PASSWORD" http://127.0.0.1:4096/global/health`（开了基本认证后这个路径也要口令，所以用 exec 探针而不是 httpGet）。
- 停机：SIGTERM → opencode 退出 → WAL 合并 → `backup/` 留一份 → 删 `.owner`。60 秒宽限。
- 日志：全部 stdout；启动时会打印独占标记、数据库自检、日志清理三行，出问题先看这三行。

## 6. 本地验证怎么跑（Mac，OrbStack Kubernetes，上下文 `orbstack`）

```bash
scripts/build-images.sh dev
kubectl --context orbstack apply -k k8s/local            # base + hostPath PV + 本地 agent-config + 放行宿主网关的策略
kubectl --context orbstack -n gaussdb-agent create secret generic model-api --from-literal=MODEL_API_KEY=local-dummy
bash scripts/k8s/local-grmp-signing.sh                    # 起开签名校验的 mock 8782、8781 转发过去、Secret grmp-sm2(测试密钥对);mock 只绑 127.0.0.1
mkdir -p /Users/Shared/agent-nas-k8s/users/u1001 /Users/Shared/agent-nas-k8s/users/u1002 /Users/Shared/agent-nas-k8s/users/u2001 /Users/Shared/agent-nas-k8s/admins/u2001
cp -R ~/kb-verify/modes/kb /Users/Shared/agent-nas-k8s/kb && chmod -R a+rwX /Users/Shared/agent-nas-k8s
for u in u1001 u1002 u2001; do python3 scripts/k8s/provision.py $u --auto-role; done
bash scripts/k8s/verify-cluster.sh                       # 13 项
```

> 本地 NAS 目录**不要放在 `/tmp` 下**：macOS 每天 0 点删掉 `/tmp` 里三种时间都超过 3 天的文件，报告、闲置账号的历史会整批消失（2026-10-24 测试环境就是这样丢的）。`/Users/Shared` 不被清理。

验证覆盖：A/B 隔离、删 Pod 重建后状态都在、管理员写入 → runtime 立刻可查、runtime 对 kb 只读、写坏 db 后从备份恢复、会话 e2e 20 例在 Pod 内、NetworkPolicy 生效（OrbStack 的 CNI 会强制执行）。

## 7. 尚未覆盖

- 网关本身（鉴权、代理、闲置回收）：平台做，本仓库只给 `provision.py` 作参考。
- frontend 镜像：阶段 6。
- NFS 上的压测与 `mountOptions` 生效验证：客户环境（spec §6.2 ⑥）。
