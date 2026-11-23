# 现场测试：SSO 接好之前，用「模拟 SSO」在浏览器里登录

适用：网关形态。贵方 SSO 还没对接，但想先在现场用浏览器把大盘和对话页走一遍。
全程约 5 分钟，不改集群里的任何配置。**只用于测试，不能用于生产。**

---

## 0. 原理

网关只认请求头里的工号（默认 `X-Agent-User`），而且要求同时带上与它约定的信任凭据（`X-Agent-Trust`），
否则一律回 403「无法确认身份」。生产环境里这两个头由贵方 SSO / 反向代理在认完人之后注入；
浏览器自己加不了，所以 SSO 接好之前，由一个本机小程序 `sso-sim.py` 站在 SSO 的位置上：

```
浏览器 ──► 127.0.0.1:18090   sso-sim.py(本机)
              │  丢掉浏览器自带的身份头,加上 工号头 + 信任凭据
              ▼
           127.0.0.1:18089   kubectl port-forward(本机)
              │  经 k8s API 打通到集群里
              ▼
           网关 Service :80 ──► 网关容器 :8080 ──► 该工号的诊断容器 :4096 / :4097
```

两个端口都在操作机本机，浏览器必须和它们在同一台机器上。端口号可以换，下文按 18089 / 18090 写。

---

## 1. 前提

| 项 | 怎么确认 |
|---|---|
| 网关、前端已就绪 | `kubectl -n gaussdb-agent get deploy gateway frontend`，READY 都不为 0 |
| 操作机能用 kubectl，且有权限读 Secret、做 port-forward | `kubectl -n gaussdb-agent auth can-i get secret/gateway-auth` 与 `kubectl -n gaussdb-agent auth can-i create pods/portforward` 都回 `yes` |
| 操作机有 Python 3.8 及以上 | `python3 --version`（Windows 上可能是 `python` 或 `py -3`）。只用标准库，不用装任何包 |
| 网关配了**信任凭据** | `kubectl -n gaussdb-agent get secret gateway-auth -o jsonpath='{.data.TRUST_SECRET}' \| wc -c` 不为 0 |
| 有 `sso-sim.py` | 离线包解开后在 `k8s/tools/sso-sim.py`；或从公开仓库下载：`curl -fsSLO https://raw.githubusercontent.com/sqlrush/gh_gs_k8s_agent/main/k8s/tools/sso-sim.py` |

**网关只配了信任网段（`TRUST_CIDRS`）、没配信任凭据时，这个办法不通。** 经 port-forward 进来的请求，
网关看到的来源地址是 `127.0.0.1`（已实测），不在贵方 SSO 的网段里，会被拒成 403。
处理办法是给 `gateway-auth` 加一个 `TRUST_SECRET`（至少 16 个字符），然后重启网关
（部署手册阶段 3.2）。网关的两道门**满足一道即可**，加了凭据不影响原来按网段放行的 SSO。
不要为此把 `127.0.0.1` 加进信任网段。

---

## 2. 操作步骤

下面的命令在 Linux / macOS 终端或 Windows 的 Git Bash 里都能用。需要两个终端窗口。

### ① 开通测试工号（已开通的跳过）

开通 = 创建这个工号的中间件令牌 Secret（与部署手册 §9.1 相同）：

```bash
kubectl -n gaussdb-agent create secret generic grmp-token-u0001 \
  --from-literal=GRMP_AUTH_TOKEN='<该工号的中间件令牌>'
```

### ② 把信任凭据存成本机文件（不在屏幕上显示）

```bash
kubectl -n gaussdb-agent get secret gateway-auth -o jsonpath='{.data.TRUST_SECRET}' | base64 -d > ~/.agent-trust
chmod 600 ~/.agent-trust
wc -c ~/.agent-trust          # 应不小于 16
```

### ③ 窗口一：把网关接到本机 18089

```bash
kubectl -n gaussdb-agent port-forward svc/gateway 18089:80
```

应看到 `Forwarding from 127.0.0.1:18089 -> 8080`。**这个窗口保持打开**，关掉就断了。

### ④ 窗口二：启动模拟 SSO

```bash
python3 k8s/tools/sso-sim.py 18090 18089 u0001 ~/.agent-trust
```

应看到 `模拟 SSO:http://127.0.0.1:18090 → 网关 127.0.0.1:18089,工号 u0001(头名 X-Agent-User)`。这个窗口也保持打开。

### ⑤ 验证，然后打开浏览器

```bash
curl -s -m 150 http://127.0.0.1:18090/global/health
```

应返回 `{"healthy":true,"version":"1.18.27"}`。第一次登录网关要为这个工号新建容器，镜像已在节点上时约 6 秒，最长等 2 分钟。

浏览器打开：

| 看什么 | 地址 |
|---|---|
| 四个大盘 | `http://127.0.0.1:18090/dash/health`（另有 `/dash/topsql`、`/dash/wdr`、`/dash/kb`） |
| 对话页 | `http://127.0.0.1:18090/L2RhdGEvc3RhdGUvd29ya3NwYWNl` |

对话页要用上面这个地址。后半段是工作目录 `/data/state/workspace` 的 base64url 编码，也就是 opencode 的项目路由；
直接打开 `http://127.0.0.1:18090/` 看到的项目列表是浏览器本地记的，换一个浏览器就是空的。

---

## 3. 常见的变化

**几个工号同时测**：每个工号起一个 `sso-sim.py`，监听端口不同，共用同一个 port-forward：

```bash
python3 k8s/tools/sso-sim.py 18091 18089 u0002 ~/.agent-trust
```

**测知识库管理员**：在最后加第 5 个参数，带上角色（以 `gateway-config` 的 `KB_ADMIN_ROLES` 为准，默认 `kb-admin`）：

```bash
python3 k8s/tools/sso-sim.py 18092 18089 u0003 ~/.agent-trust kb-admin
```

网关会为这个工号另建一个知识库导入容器 `kb-import-u0003`（`kubectl -n gaussdb-agent get deploy` 能看到）。
对话页默认进诊断容器；浏览器打开 `http://127.0.0.1:18092/kb-import/` 切到导入容器，`/runtime/` 切回（接入手册 §2.5）。

**贵方改过工号头 / 角色头的名字**（`gateway-config` 的 `USER_HEADER` / `ROLES_HEADER`）：模拟 SSO 要用同样的名字，否则 403：

```bash
SSO_USER_HEADER='X-Employee-Id' SSO_ROLES_HEADER='X-Groups' python3 k8s/tools/sso-sim.py 18090 18089 u0001 ~/.agent-trust
```

**端口被占用**：换一个监听端口即可，浏览器地址跟着换。

---

## 4. 看到什么、说明什么

| 看到 | 原因 | 处理 |
|---|---|---|
| 403「无法确认身份。请经统一认证入口访问。」 | 信任凭据不对；或网关只配了信任网段（见 §1）；或头名与网关配置不一致；或绕过了模拟 SSO 直接访问 18089 | 重做步骤 ②；核对 §1 与 §3 的头名 |
| 503「您的环境尚未开通」 | 这个工号没有 `grmp-token-<工号>` | 做步骤 ① |
| 等满 2 分钟后 504，或浏览器一直「正在启动」 | 网关建了容器，但它没就绪 | `kubectl -n gaussdb-agent describe pod -l user=u0001 \| tail -20`，看 Events：多为 NAS 挂载失败、拉不到镜像 |
| 502「模拟 SSO 连不上网关」 | 窗口一的 port-forward 断了（窗口被关，或网关 Pod 重启过） | 重做步骤 ③ |
| 浏览器打不开 `127.0.0.1:18090` | 窗口二的 `sso-sim.py` 没在运行；或浏览器不在这台机器上 | 重做步骤 ④；浏览器必须在操作机本机 |

---

## 5. 用完收尾

```bash
# 两个窗口各按 Ctrl-C;然后删掉本机的凭据文件
rm -f ~/.agent-trust
# 不再测这个工号时,回收它的容器(NAS 上的历史保留,下次登录挂回来)
kubectl -n gaussdb-agent delete deploy,svc runtime-u0001 --ignore-not-found
# 若测过管理员,另回收导入容器
kubectl -n gaussdb-agent delete deploy,svc kb-import-u0003 --ignore-not-found
```

---

## 6. 安全须知

- **信任凭据等于「能冒充任何工号」的钥匙。** 只放在操作机上权限为 600 的文件里，不要发送、不要提交、不要贴进工单，用完即删。
  Windows 上 `chmod` 不一定生效，请放在只有本人能访问的目录。
- **模拟 SSO 只监听 `127.0.0.1`，但这台机器上任何能连上监听端口的人或程序，都会以这个工号的身份使用系统**，
  包括这个工号能诊断的数据库。测完立刻停掉，不要长期挂着。
- **不能用于生产。** 生产环境必须由贵方 SSO 认人之后注入工号头（接入手册 §2）。
