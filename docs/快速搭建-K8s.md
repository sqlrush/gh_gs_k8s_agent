# 快速搭建（K8s 测试环境）

按顺序执行。每条命令下面是**执行后应该看到的结果**。

前置：一台 Linux 跳板机，装好 `docker` 与 `kubectl`，能连到 K8s 集群。
需要准备的信息见最后一节「要填的值」。

---

## 1. 登录并拉镜像

```bash
docker login ghcr.io -u <你的GitHub用户名>
```
> 提示 Password 时粘贴令牌（令牌在 https://github.com/settings/tokens/new 生成，只勾 `read:packages`）。看到 `Login Succeeded`。

```bash
docker pull ghcr.io/sqlrush/gaussdb-agent-runtime:agent-v1.5.1-oc1.18.27
```

```bash
docker pull ghcr.io/sqlrush/gaussdb-agent-kb-import:agent-v1.5.1-oc1.18.27
```

```bash
docker pull ghcr.io/sqlrush/gaussdb-agent-gateway:agent-v1.5.1-oc1.18.27
```

```bash
docker pull ghcr.io/sqlrush/gaussdb-agent-frontend:agent-v1.5.1-oc1.18.27
```

```bash
docker pull ghcr.io/sqlrush/gaussdb-agent-kbstd:agent-v1.5.1-oc1.18.27
```

```bash
docker images | grep gaussdb-agent
```
> 5 行（第五个 `gaussdb-agent-kbstd` 是标准知识库：GaussDB 官方文档集，部署后由 Job 自动装进知识库）。

---

## 2. 取源码

```bash
git clone https://github.com/sqlrush/gh_gs_k8s_agent.git && cd gh_gs_k8s_agent
```
> 进入目录。**之后所有命令都在这里执行。**

---

## 3. 推到内网仓库

```bash
REG=<内网镜像仓库地址>
```

```bash
docker tag ghcr.io/sqlrush/gaussdb-agent-runtime:agent-v1.5.1-oc1.18.27 $REG/gaussdb-agent-runtime:agent-v1.5.1-oc1.18.27
docker tag ghcr.io/sqlrush/gaussdb-agent-kb-import:agent-v1.5.1-oc1.18.27 $REG/gaussdb-agent-kb-import:agent-v1.5.1-oc1.18.27
docker tag ghcr.io/sqlrush/gaussdb-agent-gateway:agent-v1.5.1-oc1.18.27 $REG/gaussdb-agent-gateway:agent-v1.5.1-oc1.18.27
docker tag ghcr.io/sqlrush/gaussdb-agent-frontend:agent-v1.5.1-oc1.18.27 $REG/gaussdb-agent-frontend:agent-v1.5.1-oc1.18.27
docker tag ghcr.io/sqlrush/gaussdb-agent-kbstd:agent-v1.5.1-oc1.18.27 $REG/gaussdb-agent-kbstd:agent-v1.5.1-oc1.18.27
```

```bash
docker push $REG/gaussdb-agent-runtime:agent-v1.5.1-oc1.18.27
docker push $REG/gaussdb-agent-kb-import:agent-v1.5.1-oc1.18.27
docker push $REG/gaussdb-agent-gateway:agent-v1.5.1-oc1.18.27
docker push $REG/gaussdb-agent-frontend:agent-v1.5.1-oc1.18.27
docker push $REG/gaussdb-agent-kbstd:agent-v1.5.1-oc1.18.27
```
> 每条最后一行是 `digest: sha256:...`。

```bash
sed -i "s#image: gaussdb-agent-#image: $REG/gaussdb-agent-#" k8s/base/gateway.yaml k8s/base/frontend.yaml k8s/base/kbstd-install.yaml
sed -i "s#USER_IMAGE_REGISTRY: \"\"#USER_IMAGE_REGISTRY: \"$REG\"#" k8s/base/configmap-gateway.yaml
```

```bash
grep -rh "image:\|USER_IMAGE_REGISTRY:" k8s/base/ | grep -v "^ *#"
```
> 每行地址都以 `$REG` 开头，`USER_IMAGE_REGISTRY` 也是 `$REG`（网关按它给用户建容器；
> 用户容器的镜像名是网关拼出来的，不在任何清单文件里，所以不能靠 sed 清单改）。

---

## 4. 接上存储

```bash
cp k8s/base/nas-pv-nfs.example.yaml k8s/base/nas-pv-nfs.yaml
vi k8s/base/nas-pv-nfs.yaml
```

只改最后两行：

```yaml
  nfs:
    server: <NFS服务器地址>
    path: <NFS导出路径>
```

> `vi` 保存退出：按 `Esc`，输入 `:wq` 回车。

```bash
kubectl apply -f k8s/base/nas-pv-nfs.yaml
```
> `persistentvolume/nas-nfs created`

> 存储管理员需先在 NFS 导出目录下建好 `users/`、`admins/`、`kb/` 三个目录，属主 uid 1000。
> 属主不对的后果是**能对话、关掉浏览器历史全没了**，而且当时没有任何报错。

---

## 5. 创建资源

```bash
kubectl apply -k k8s/base
```

```bash
kubectl -n gaussdb-agent get pvc
```
> `nas` 的 STATUS 是 `Bound`。

---

## 6. 创建密钥

```bash
read -rs -p "模型服务密钥: " V && echo
kubectl -n gaussdb-agent create secret generic model-api --from-literal=MODEL_API_KEY="$V"
unset V
```

```bash
TRUST=$(openssl rand -hex 24)
kubectl -n gaussdb-agent create secret generic gateway-auth --from-literal=TRUST_SECRET="$TRUST" --from-literal=ADMIN_TOKEN="$(openssl rand -hex 24)"
echo "$TRUST"
```
> 把这串 48 位字符**抄下来**，第 9 步要用。

```bash
kubectl -n gaussdb-agent get secret
```
> `model-api`、`gateway-auth` 两行。

---

## 7. 填配置

```bash
kubectl -n gaussdb-agent edit configmap agent-config
```

改这四行引号里的内容：

```yaml
  GRMP_API_HOST: "<中间件地址>"
  GRMP_API_PORT: "<中间件端口>"
  GRMP_PATH_PREFIX: "<中间件接口路径前缀>"
  MODEL_BASE_URL: "<模型服务地址,含/v1>"
  MODEL_ID: "<模型名称>"
```

```bash
kubectl -n gaussdb-agent edit configmap gateway-config
```

确认这行：

```yaml
  USER_IMAGE_TAG: "agent-v1.5.1-oc1.18.27"
```

---

> 知识库**不用单独起数据库**：向量 + 词法索引是 NAS 上的 `kb/index/store.sqlite`（SQLite + sqlite-vec，已装在镜像里），
> 管理员在导入容器里导入时自动建（`kb.py index`），诊断容器只读。建索引之前检索走文件模式，不影响使用。

---

## 8. 启动网关

```bash
kubectl -n gaussdb-agent rollout restart deploy/gateway
kubectl -n gaussdb-agent rollout status deploy/gateway --timeout=180s
```

```bash
kubectl -n gaussdb-agent logs -l app=agent-gateway --tail=3
```
> 出现一行以「网关就绪」开头的中文。

```bash
kubectl -n gaussdb-agent rollout status deploy/frontend --timeout=180s
```
> `deployment "frontend" successfully rolled out`（大盘前端，第 5 步 apply 时已建，这里等它起来）。

---

## 9. 开通用户并验证

```bash
read -rs -p "该工号的 GRMP 令牌: " V && echo
kubectl -n gaussdb-agent create secret generic grmp-token-<工号> --from-literal=GRMP_AUTH_TOKEN="$V"
unset V
```

```bash
kubectl -n gaussdb-agent port-forward svc/gateway 18080:80 &
sleep 3
```

```bash
STAFF=<工号>
curl -s http://127.0.0.1:18080/healthz
```
> `{"ok": true, "version": "gaussdb-agent-gateway"}`

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Agent-User: $STAFF" http://127.0.0.1:18080/session
```
> `403`

```bash
curl -s -m 150 -o /dev/null -w "%{http_code}\n" -H "X-Agent-Trust: $TRUST" -H "X-Agent-User: $STAFF" http://127.0.0.1:18080/global/health
```
> `200`（首次要等 10–30 秒，网关在建环境）。
> **命令挂着超过 30 秒没返回，说明这个人的容器起不来**：网关会原地等它最多 120 秒，这期间屏幕上什么都没有，到时才返回 `504`。不用等满，另开终端执行 `kubectl -n gaussdb-agent get pod | grep $STAFF` 看状态，`ImagePullBackOff` 是镜像没推或标签不对，`Pending` 看 `kubectl -n gaussdb-agent describe pod -l user=$STAFF | tail -20`。

```bash
kubectl -n gaussdb-agent get deploy runtime-$STAFF
```
> `READY` 列是 `1/1`。

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Agent-Trust: $TRUST" -H "X-Agent-User: $STAFF" http://127.0.0.1:18080/dash/health
```
> `200`（大盘经网关可达。页面上有没有内容取决于这个人跑没跑过健康检查）

**到这里测试环境就绪。**

---

## 要填的值

| 值 | 找谁要 |
|---|---|
| GitHub 用户名与令牌 | 自己生成，令牌只勾 `read:packages` |
| 内网镜像仓库地址 | 容器平台管理员 |
| NFS 服务器地址、导出路径 | 存储管理员 |
| 模型服务地址、密钥、模型名 | AI 平台管理员 |
| 中间件地址、端口 | 中间件团队 |
| 工号 + 该工号的 GRMP 令牌 | 中间件团队 |

---

## 命令没对上时

```bash
kubectl -n gaussdb-agent get pod
kubectl -n gaussdb-agent logs -l app=agent-gateway --tail=30
```

把这两条的完整输出发我方。

详细排查见 `命令卡-测试环境搭建.md`，架构与 SSO 对接见 `接入手册-SSO与K8s.md`，
各团队分工与验收见 `对接清单-各方要做什么.md`，参数逐项说明见 `参数手册.md`。
