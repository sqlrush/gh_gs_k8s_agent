# 单机演示（不用 K8s）

一台装了 Docker 的机器，三条命令跑起来一个能用的智能体。
**给演示和试用用**，不是给多人共用的环境 —— 那个要用 K8s，见 `快速搭建-K8s.md`。

本篇每条命令下面的输出都是实际跑出来的。

---

## 能演示什么、不能演示什么

| | |
|---|---|
| ✅ 完整的对话界面与全部诊断技能 | |
| ✅ 状态持久化：容器删掉重建，历史还在 | |
| ✅ 知识库（文件模式） | 不需要任何数据库。runtime 镜像不建索引，检索直接在 `kb/` 文件上做 |
| ✅ 异常终止的行为（留快照 + 告警） | |
| ❌ 一人一容器、按工号隔离 | 这部分是接入网关做的，要 K8s |
| ❌ 闲置自动回收 | 同上 |
| ❌ 四个大盘 | 大盘页面经网关按工号取本人的报告，要 K8s。单机下技能报告照样存档在 `reports/`，只是没有页面看 |
| ❌ 语义检索 | 要导入容器（kb-import）用 `kb.py index` 建出索引 `kb/index/store.sqlite`，再加 embedding 端点 |

---

## 需要准备

| 要的东西 | 找谁 | 没有它会怎样 |
|---|---|---|
| 一台装了 Docker 的 Linux 机器 | — | |
| runtime 镜像 | 见 `快速搭建-K8s.md` 第 1 步 | |
| 模型服务地址、密钥、模型名 | AI 平台 | 容器**起不来**，日志指名缺哪个 |
| 中间件地址 + 一个令牌 | 中间件团队 | 容器能起来、界面能开，一登录数据库就失败 |

地址要含 `/v1`。先只演示界面的话，中间件那两项可以随便填。

---

## 1. 建目录

```bash
mkdir -p ~/agent-demo/me ~/agent-demo/kb
```

这两个目录就是「NAS」。`me` 放会话、历史、连接配置，`kb` 放知识库。
**容器删了它们还在**，这是下面第 4 步要演示的。

```bash
sudo chown -R 1000:1000 ~/agent-demo
```

> 容器以 uid 1000 运行。属主不对的后果是：**容器正常启动、界面正常打开、
> 关掉浏览器历史全没了**，而且当时没有任何报错。这条不要跳过。

## 2. 启动

把 `<>` 四处换成实际值，整段粘贴执行：

```bash
docker run -d --name agent-demo \
  -e GSDB_USER_ID=demo \
  -e OPENCODE_SERVER_PASSWORD='<自己定一个口令>' \
  -e MODEL_BASE_URL='<模型服务地址,含/v1>' \
  -e MODEL_API_KEY='<模型服务密钥>' \
  -e MODEL_ID='<模型名>' \
  -e GRMP_API_HOST='<中间件地址>' \
  -e GRMP_API_PORT=8080 \
  -e GRMP_PATH_PREFIX='<中间件接口路径前缀>' \
  -e GRMP_AUTH_TOKEN='<中间件令牌>' \
  -v ~/agent-demo/me:/nas/me \
  -v ~/agent-demo/kb:/nas/kb:ro \
  -p 14096:4096 \
  gaussdb-agent-runtime:<镜像标签>
```

等十几秒，看状态：

```bash
docker ps --filter name=agent-demo --format "{{.Status}}"
```

> ```
> Up 12 seconds (healthy)
> ```

**必须是 `healthy`。** 看日志确认启动做了哪几件事：

```bash
docker logs agent-demo
```

> ```
> 独占标记: 本 Pod 64c6e5ef1ece 持有(新建)
> 状态已加载到本地:状态同步:文件 0(0.0 MB)· 库 0 · 删除 0
> 数据库自检: /data/state/xdg-data/opencode/opencode.db 不存在,首次启动,opencode 将新建
> 日志清理: 删除 0 个超过 7 天的 .log
> 工作目录: 已初始化为 git 仓库(/data/state/workspace)
> opencode server listening on http://0.0.0.0:4096
> ```

首次启动「文件 0 · 库 0」是对的 —— NAS 上还没有东西可加载。

## 3. 打开界面

浏览器访问 `http://<这台机器的地址>:14096/`

- 用户名 **`opencode`**
- 口令就是上面 `OPENCODE_SERVER_PASSWORD` 填的那个

先不开浏览器的话，命令行验证鉴权生效：

```bash
curl -s -o /dev/null -w "不带口令 %{http_code}\n" http://127.0.0.1:14096/
curl -s -o /dev/null -w "带口令   %{http_code}\n" -u opencode:'<你的口令>' http://127.0.0.1:14096/
```

> ```
> 不带口令 401
> 带口令   200
> ```

界面里发一句「帮我看下数据库有没有慢 SQL」就能走完一次诊断。
第一次会让你提供数据库连接信息，按提示答即可。

## 4. 演示状态持久化

这是单机演示最值得看的一段：**容器是可以随便删的，数据不在容器里。**

先造一点内容（正常用的时候这就是你的对话历史）：

```bash
docker exec agent-demo sh -c 'echo 停机前写的内容 > /data/state/workspace/demo.txt'
```

优雅停机 —— 看它收尾做了什么：

```bash
docker stop agent-demo && docker logs agent-demo | tail -4
```

> ```
> WAL 已合并回主库(/data/state/xdg-data/opencode/opencode.db)
> 收尾同步完成并已清 dirty 标记。状态同步:文件 26(0.0 MB)· 库 1 · 删除 0
> 退出备份: opencode.db.20260922T015807Z(保留最近 3 份)
> opencode 已退出(rc=143),状态已完整回写到 NAS
> ```

**彻底删掉容器**，再看 NAS 上还剩什么：

```bash
docker rm agent-demo && ls ~/agent-demo/me/
```

> ```
> backup  gdaa  workspace  xdg-data  xdg-state
> ```

用**完全相同**的那条 `docker run` 命令重建（容器名改成 `agent-demo2`）：

```bash
docker logs agent-demo2 | head -3
```

> ```
> 独占标记: 本 Pod 5e75dea3e792 持有(新建)
> 状态已加载到本地:状态同步:文件 26(0.0 MB)· 库 1 · 删除 0
> 数据库自检: 完整性检查通过(/data/state/xdg-data/opencode/opencode.db)
> ```

26 个文件和那个库从 NAS 加载回来了。界面打开，历史都在。

```bash
docker exec agent-demo2 cat /data/state/workspace/demo.txt
```

> ```
> 停机前写的内容
> ```

## 5. 演示异常终止会发生什么

这一段演示的是**我方承认的那个代价**：正常停机零丢失，被强杀会丢最后一个同步周期
（默认 60 秒）。演示它，是因为客户迟早会遇到，提前看过比事后解释好。

```bash
docker exec agent-demo2 sh -c 'echo 强杀前写的 > /data/state/workspace/killed.txt'
docker kill agent-demo2 && docker rm agent-demo2
ls ~/agent-demo/me/workspace/
```

> ```
> demo.txt
> ```

`killed.txt` 不在 —— 它还没到回写周期就随容器消失了。这是预期行为。

现在重建（同一条 `docker run`，名字改成 `agent-demo3`）。**第一次会失败**：

```bash
docker ps -a --filter name=agent-demo3 --format "{{.Status}}"
docker logs agent-demo3 | tail -1
```

> ```
> Exited (1) 23 seconds ago
> 独占标记: 另一个 Pod 5e75dea3e792 仍持有 /nas/me/.owner(66 秒前刷新),不启动
> ```

**这是对的，不是故障。** 被强杀的容器没有机会释放独占标记，新容器不知道那个容器
已经死了 —— 它只看到「有人还持有这个目录」。宁可不启动，也不要两个容器同时
读写同一份数据库。标记满 90 秒过期后就能接管：

```bash
docker start agent-demo3     # 等 90 秒后再执行
docker logs agent-demo3 | head -3
```

> ```
> 独占标记: 本 Pod 4c3b76422091 持有(接管过期标记 5e75dea3e792)
> ⚠ 上次是**非优雅**退出(dirty 标记还在):NAS 上那份已先快照到 me/.unclean-1790042533/。
>   这次启动会用它继续,但上次最后一个同步周期内的对话可能已丢失。
> 状态已加载到本地:状态同步:文件 26(0.0 MB)· 库 1 · 删除 0
> ```

三件事都发生了：接管、**明确告警**、在 NAS 上留下当时的快照。

```bash
ls -d ~/agent-demo/me/.unclean-*
```

> ```
> <你的家目录>/agent-demo/me/.unclean-1790042533
> ```

`.unclean-*` 目录不会自动清理。确认不需要后可以删 —— 它存在本身就是一个信号：
有容器被强杀或被驱逐过。

> **在 K8s 里不需要手工 `docker start`**：容器退出后由集群自动重启，
> 标记过期后那次重启就接管了。单机用 `docker run` 没有这个机制，所以要手动再跑一次。
> 想让它自动重试，启动时加 `--restart=on-failure`。

## 6. 独占是真的在拦

想确认第 5 步那个「不启动」不是偶然：让两个容器指向同一个目录。

```bash
docker run -d --name agent-dup ...（同样的参数，同样的 -v，不要 -p）
sleep 70
docker logs agent-dup | tail -1
```

> ```
> 独占标记: 另一个 Pod 5e75dea3e792 仍持有 /nas/me/.owner(17 秒前刷新),不启动
> ```

后来的那个退出，**原来那个不受影响**：

```bash
docker ps --filter name=agent-demo --format "{{.Status}}"
```

> ```
> Up 2 minutes (healthy)
> ```

## 7. 清理

```bash
docker rm -f agent-demo agent-demo2 agent-demo3 agent-dup 2>/dev/null
rm -rf ~/agent-demo
```

---

## 常见问题

**容器状态一直不是 healthy。** 先看日志：

```bash
docker logs agent-demo | tail -20
```

- `缺少环境变量:...` → 模型服务那三项有漏的
- 卡在「独占标记」不动 → 同一个 `me` 目录还有别的容器在用，或上次被强杀（见第 5 步）
- 起来了但写不进东西 → `me` 目录属主不是 1000，回到第 1 步

**界面打不开。** 检查端口有没有被占：`ss -lntp | grep 14096`。
换一个宿主端口就行，改 `-p` 左边那个数。

**能对话但一碰数据库就报错。** 那是中间件那两项：`GRMP_API_HOST` 和
`GRMP_AUTH_TOKEN`。容器不会因为它们填错而起不来 —— 它只在真正去连的时候才报。

**要演示给别人看，但只有一台机器。** 多人共用同一个容器是可以跑的，
但所有人共享同一份会话与历史，**互相能看到对方的对话**。
要一人一份就得上 K8s，见 `快速搭建-K8s.md`。
