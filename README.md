# gh_gs_k8s_agent

GaussDB 数据库智能体的**后端**：把 opencode 与 GaussDB / openGauss 诊断技能放进 Kubernetes。
每个用户一个隔离的环境（按需建 Pod，或固定池化的槽位），状态全部在 NAS 上；接入网关负责认身份、分配环境、反向代理。

本仓库是**联合项目**：我方负责后端，前端方负责前端，双方（含各自的编码智能体）在同一个仓库里协作。

## 发布出口（v1.5.0 起）

**本仓库是对外发布的唯一出口。** 每个发布版本在这里同时提供下面几样，版本号一致：

| 内容 | 位置 | 标签 |
|---|---|---|
| 完整源码（构建全部镜像所需） | `main` 的 `agent/`、`docker/`、`frontend/`、`gateway/`、`k8s/`、`release/`、`scripts/`、`acceptance/`、`delivery/` | `agent-v1.5.2` |
| Fuyao 沙箱模板材料 | `main` 的 [`e2b/`](e2b/)（模板注册脚本、临时仓库、`Dockerfile.e2b`，与离线包的 `e2b/` 逐字节相同） | `agent-v1.5.2` |
| 面向客户的手册与白名单 | `main` 的 [`docs/`](docs/)（白名单在 `docs/delivery/`） | `agent-v1.5.2` |
| 诊断技能包 / 知识库导入技能包 | 分支 `diag` / `kbimport`（见下表） | `diag-v1.5.2` / `kbimport-v1.5.2` |
| 容器镜像与沙箱模板镜像 | `ghcr.io/sqlrush/gaussdb-agent-*:agent-v1.5.2-oc1.18.27`（见下文「Fuyao 沙箱运行模式」） | — |
| 离线交付包（全部文件：镜像、沙箱模板 OCI 包、K8s 清单与手册、发版台、`release.yaml`、校验文件） | [发布页 agent-v1.5.2](https://github.com/sqlrush/gh_gs_k8s_agent/releases/tag/agent-v1.5.2) 的附件，下载后 `shasum -a 256 -c *.sha256` 核对 | `agent-v1.5.2` |

代码在开发仓库 [gh_agent_k8s](https://github.com/sqlrush/gh_agent_k8s) 开发，每次发版整体同步到本仓库 `main` 并打同名标签；两个技能包分支由转化脚本 `delivery/transform.py` 从同一发布标签自动生成。**不要在本仓库直接改代码或这两个分支**（转化脚本发现手改会停下），改动在开发仓库做、下次发版同步过来：

| 分支 / 标签 | 内容 | 用在 |
|---|---|---|
| [`diag`](https://github.com/sqlrush/gh_gs_k8s_agent/tree/diag) / `diag-v1.5.2` | 诊断技能包：`AGENTS.md`、`common/`、17 个诊断技能 + `gaussdb-kb`、`scripts/registry/`（白名单脚本定义）、`whitelist/`（133 条白名单 DML、历次增量与清单；从 v1.5.1 升级导 `scripts-delta-20261125.sql`（新增 1 条、更新 21 条：在业务库执行的脚本输出列改成 M 兼容库能原样取回的类型，布尔列输出 1/0、名字类列转成文字；新增 YAML 格式执行计划），从 v1.5.0 升级先导 `scripts-delta-20261123.sql` 再导 20261125；从 v1.5.0 升级到 v1.5.1 时导的是 `scripts-delta-20261123.sql`（更新 13 条：M 兼容业务库上报错的脚本改成 A / M 两种兼容模式都能执行的写法，登录探针多取兼容模式），从 v1.4.14 升级先导 `scripts-delta-20261119.sql` 再导 20261123；从 v1.4.14 升级到 v1.5.0 时导的是 `scripts-delta-20261119.sql`（更新 4 条：锁堵塞成对明细、统计信息过期、未使用的索引、内核函数探测的拼接写法），从 v1.4.13 升级先导 `scripts-delta-20261116.sql`（更新 2 条：死元组清单、锁堵塞成对明细的时间差写法）再导 20261119，从 v1.4.11 / v1.4.12 升级先导 `scripts-delta-20261112.sql`（新增 2 条退路脚本；原来 8 条两段 SQL 的脚本不再使用，可以保留）再依次导 20261116、20261119，从 v1.4.10 升级先导 `scripts-delta-20261111.sql`（新增 1、更新 71 条执行库）再导 20261112，从 v1.4.7 ~ v1.4.9 升级依次导 `scripts-delta-20261108.sql`、20261111、20261112，从 v1.4.3 ~ v1.4.6 升级先导 `scripts-delta-20261105.sql`、再依次导 20261108、20261111 与 20261112；更早的版本按上面顺序导完后再依次导 20261116、20261119、20261123、20261125；不确定导到哪一份时，把白名单导出交给我方核对）、`rootspec/search_docs.py`（根规范检索脚本）、`补充说明.md` | 诊断环境 |
| [`kbimport`](https://github.com/sqlrush/gh_gs_k8s_agent/tree/kbimport) / `kbimport-v1.5.2` | 知识库导入技能包：`AGENTS.md`、`common/`、`gaussdb-kb-import`、`gaussdb-kb-init`、`gaussdb-kb`、`rootspec/search_docs.py`、`补充说明.md` | 知识库导入环境 |

v1.4.7 起诊断站在应用 DBA / 运维 DBA 的角度（指标异常追到引起它的 SQL、会话、应用、表；处置先改应用、再做运维操作，参数只在典型场景给）。v1.4.8 修复 api 模式登录：中间件按实例 IP 固定连一个库、不按库名切换，原来填业务库名一律登录不了；v1.4.9 起库名与 schema 的限制全部放开（库名核对不拦登录；同名表跨 schema 取一个照常跑），中间件固定连 postgres 时业务库的 SQL 按执行历史调优。v1.4.11 按行方切库方案调用中间件时带 `dbName`：登录时给了业务库（`--database <业务库>`），表、索引、执行计划、统计信息、存储过程就在该业务库上取，SQL 执行历史与 WDR 快照仍在 postgres；白名单要导本版增量，登录看到「已直连」即生效。v1.4.12 起行方白名单没导入该增量时也不报错（取数在 postgres 上），查不到慢存储过程时说清是没切到业务库、参数关着还是还没有调用记录。v1.4.13 是上生产前的稳定版：界面上不出现报错原文（中间件地址、接口路径、实例 ID 不显示，原文进本机诊断日志、报错带详情编号），某一项取不到时写「这一部分无法查询」、其余照常给出；不再发两段 SQL；视图没数据时换 SQL 取。v1.4.14 新增交互耗时分析：`python3 $SK/common/timing/cli.py --last 3 --html /tmp/耗时报告.html` 统计一次提问里每一步（模型等首字、思考与输出、工具中的 GRMP 与数据库、运行时空档）用了多久并写出慢在哪，纯脚本统计、不调模型；修复死元组清单与锁堵塞成对明细在 M 兼容业务库上报 un support type。v1.5.0：部署自检不再中途退出（技能目录里漏放 `scripts/` 时，白名单两项标「无法比对」并说明怎么补，其余照常检查）；4 条在业务库执行的脚本把 `||` 拼接改为 `concat`，修复 M 兼容业务库上报「argument of OR must be type boolean」；安装时 `scripts/` 与 `common/` 放同一层、设置 `GSDB_SKILLS_DIR` 列为必做项；cite-check 认官方文档集的引用；部署自检与发版台按白名单实际缺口列出要依次导入的增量；后端新增 Fuyao 沙箱运行模式（预览）与资源大盘（见下文），只装技能包的环境不受影响。v1.5.1：业务库 M 兼容——现场在 M 兼容业务库上报错的诊断脚本（分区体检、表写入计数、统计信息过期、SQL 审查的表 / 索引清单、过程模糊查找等）改成 A / M 两种兼容模式都能执行的写法；登录识别业务库兼容模式并写进登录输出，M 库上存储过程源码不走 ADM_SOURCE、语句检查与取表名按 MySQL 词法；模型按兼容模式写改写语句（根提示词加 3 行、SQL 调优与存储过程调优的技能说明各加半句、新增 M 兼容写法参考）；部署自检加 `--m-trial`，把 41 条业务库脚本各试跑一次（升级后请跑一次并把输出发回）。v1.5.2：业务库 M 兼容第二轮——M 兼容业务库上中间件把布尔列取成 `[B@…`（原值丢失），健康检查报「无法识别的布尔取值」、SQL 调优与 vacuum 报错、SQL 审查把每张表判成没有主键；在业务库执行的脚本输出列全部改成 M 兼容库能原样取回的类型（布尔输出 1/0、名字类转成文字），新增 YAML 格式执行计划供 M 库上的代价推演用，布尔解析认不出就报错，部署自检 `--m-trial` 检查取回来的值（升级后请在 M 兼容业务库上跑一次并把输出发回）。提示词未改。与 v1.5.1 的代码差异见 [docs/delivery/v1.5.1-v1.5.2-代码差异.md](docs/delivery/v1.5.1-v1.5.2-代码差异.md)；与 v1.5.0 的代码差异见 [docs/delivery/v1.5.0-v1.5.1-代码差异.md](docs/delivery/v1.5.0-v1.5.1-代码差异.md)，v1.4.14 → v1.5.0 见 [docs/delivery/v1.4.14-v1.5.0-代码差异.md](docs/delivery/v1.4.14-v1.5.0-代码差异.md)。每个分支根目录的 `补充说明.md` 写着：版本对应、包内容与技能版本、依赖（Python 包与系统程序）、环境变量（必填 / 默认值）、SQL 白名单与上一版的差异、
根规范目录约定、变更摘要。

```bash
git clone -b diag-v1.5.2 https://github.com/sqlrush/gh_gs_k8s_agent.git gaussdb-diag          # 诊断技能包
git clone -b kbimport-v1.5.2 https://github.com/sqlrush/gh_gs_k8s_agent.git gaussdb-kbimport  # 知识库导入技能包
```

**拉下来之后要装进正在用的技能目录**，只 clone 不会生效。以技能目录 `/workspace/.opencode/skills` 为例（换成你们环境里的 `$GSDB_SKILLS_DIR`）：

```bash
SK=/workspace/.opencode/skills
cp $SK/common/config.yaml /tmp/config.yaml.bak          # 先备份本地配置(中间件地址等),包里的同名文件只是模板
cp -r gaussdb-diag/common/. $SK/common/
cp -r gaussdb-diag/scripts/. $SK/scripts/
cp -r gaussdb-diag/skills/* $SK/
cp /tmp/config.yaml.bak $SK/common/config.yaml          # 还原本地配置
cat $SK/common/VERSION                                 # v1.5.2 显示 agent-v1.5.2
```

`AGENTS.md` 放进 opencode 的配置目录。换完**新开对话**（旧对话里读进来的还是旧版技能说明）。

**两项必做**（v1.5.0 起写进补充说明）：`scripts/` 必须和 `common/` 一起放在技能目录下（上面第 3 条 `cp` 命令），部署自检要拿它和中间件登记的清单比对；启动 opencode 的环境里必须设置 `GSDB_SKILLS_DIR`（例如 `export GSDB_SKILLS_DIR=/workspace/.opencode/skills`），根提示词里的命令、部署自检与耗时统计都按它找脚本。**升级顺序**：先把白名单增量导入中间件，再换技能包。

装完后登录任一实例，在对话里说「做一次部署自检」（或运行 `python3 $SK/gaussdb-login/scripts/selfcheck.py --session <句柄>`）：它核对技能包版本、白名单是否导全、关键脚本能否执行（含执行账号权限）、数据库监控开关，以及（v1.4.11 起）本会话是否切到了业务库、行方白名单里各脚本的执行库与本版是否一致，逐项写明结论和找谁。

`main` 上的代码与发布标签 `agent-v1.5.2` 一致：`git clone -b agent-v1.5.2 https://github.com/sqlrush/gh_gs_k8s_agent.git`，按《[从源码构建到K8s运行-完整手册](docs/从源码构建到K8s运行-完整手册.md)》可以自己构建全部镜像。v1.4.6 及以前的部署上线方案照旧按各自的标签使用。

| 文档 | 给谁 | 内容 |
|---|---|---|
| [docs/部署上线方案-v1.4.6.md](docs/部署上线方案-v1.4.6.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.6/)） | 项目经理 / 各方团队 | **从拉代码到部署上线的完整方案**（agent-v1.4.6）：**Top SQL 只列业务 SQL、回答里不出现系统 SQL**；两份根提示词、根规范随镜像（推镜像前换成行内正式规范，第 6.1 节）；系统组成与安全设计、各方分工与检查单、构建（或直接拉我方发布的镜像）、白名单导入、部署、开通、验收、固定池化、知识库、统一认证与前端、**发版台**（跳板机上一条命令起：资源扫描、变更预览确认、按槽位在线替换、冒烟验证）、知识库处置的「取代」、从 v1.4.3 / v1.4.2 / v1.4.1 / v1.4.0 / v1.3.x 升级、回滚、运维，附全部 128 条白名单脚本 |
| [发版台使用手册（带截图，在线查看）](https://sqlrush.github.io/gh_gs_k8s_agent/release-console/)（源 [docs/release-console/](docs/release-console/)） | 运维 | **发版台怎么用，每一步配界面截图**：准备与启动、资源配置、资源扫描、升级计划、变更预览确认、白名单核对、池子在线替换、冒烟、发布报告、暂停 / 续跑 / 回滚、常见问题 |
| [docs/发版台-运维手册.md](docs/发版台-运维手册.md) | 运维 | 同上的文字版：跳板机要求、启动、资源配置、扫描、发布 / 续跑 / 回滚、白名单核对两种方式、常见问题 |
| [docs/部署上线方案-v1.4.5.md](docs/部署上线方案-v1.4.5.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.5/)） | 留档 | 上一版（agent-v1.4.5）的部署上线方案 |
| [docs/部署上线方案-v1.4.4.md](docs/部署上线方案-v1.4.4.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.4/)） | 留档 | 上一版（agent-v1.4.4）的部署上线方案 |
| [docs/部署上线方案-v1.4.3.md](docs/部署上线方案-v1.4.3.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.3/)） | 留档 | 上一版（agent-v1.4.3）的部署上线方案 |
| [docs/部署上线方案-v1.4.2.md](docs/部署上线方案-v1.4.2.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.2/)） | 留档 | 上一版（agent-v1.4.2）的部署上线方案 |
| [docs/部署上线方案-v1.4.1.md](docs/部署上线方案-v1.4.1.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.1/)） | 留档 | 上一版（agent-v1.4.1）的部署上线方案 |
| [docs/部署上线方案-v1.4.0.md](docs/部署上线方案-v1.4.0.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.0/)） | 留档 | 上一版（agent-v1.4.0）的部署上线方案 |
| [docs/delivery/白名单脚本清单.md](docs/delivery/白名单脚本清单.md) | 中间件团队 | 133 条诊断脚本的用途、参数与 SQL 全文；全量 `scripts.sql` 与历次升级增量都在 `docs/delivery/`（诊断技能包的 `whitelist/` 里是同一份） |
| [docs/从源码构建到K8s运行-完整手册.md](docs/从源码构建到K8s运行-完整手册.md) | 实施 / 运维 / 开发 | 从拉代码（本仓库）、构建镜像、推仓库，到部署进 K8s、开通工号、对话里跑通健康检查；可选的固定池化。每步带实测输出 |
| [docs/前端对接接口说明.md](docs/前端对接接口说明.md) | 前端 | 接入后端的全部接口（v1.1）：身份与网关行为、对话接口与事件流、报告接口、大盘链接格式、回答正文的渲染与版式、联调清单 |
| [docs/智能体验收方案.md](docs/智能体验收方案.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/acceptance/)） | 后端 / 项目经理 | **每次升级智能体怎么验收**：11 个方面 53 道题、每题的评分标准（版式、工具、要点、处置、引用、禁止项）、三层评分、失分归因到工具 / 提示词 / 模型、发版门槛；题库在 [acceptance/](acceptance/) |
| [docs/仓库结构说明.md](docs/仓库结构说明.md) | 后端 / 前端 / 双方的编码智能体 | 逐个目录说明每个文件的作用、改什么去哪里 |
| [docs/](docs/) 其余手册 | 实施 / 运维 | 《部署手册-从零到上线》《版本说明》《参数手册》《快速搭建-K8s》《快速搭建-Fuyao沙箱》《对接清单-各方要做什么》《接入手册-SSO与K8s》《固定池化手册》《功能清单-容器版》《单机演示-不用K8s》《现场测试-模拟SSO》《命令卡-测试环境搭建》，与离线包里的 HTML 手册同源 |
| [docs/api/](docs/api/) | 前端 / 后端 | 机器可读的接口定义：`opencode-1.18.27-openapi.json`（对话接口全集）、`reports-contract.json`（报告字段契约，后端单测逐条核对） |
| [docs/delivery/回答模板化-交付说明.md](docs/delivery/回答模板化-交付说明.md) | 平台 / 中间件 / 前端 | 回答模板化（2026-10-26）要做的事：白名单增量 3 条、镜像升级、前端第二种颜色、装后冒烟 |
| [docs/reports/answer-template-comparison-20261026.html](https://sqlrush.github.io/gh_gs_k8s_agent/reports/answer-template-comparison-20261026.html) | 双方 | 回答模板化前后对比：同一组 11 道题在测试环境改造前后各问一次，格式检查、耗时、生成量与原文并排（在线查看） |
| [docs/reports/qa-flow-v1.4.10-20261109.html](https://sqlrush.github.io/gh_gs_k8s_agent/reports/qa-flow-v1.4.10-20261109.html) | 双方 / 项目经理 | **一次提问的全流程：步骤、耗时与 token**（agent-v1.4.10 测试环境实测，9 道代表性题目各问 2 次）：从提问进入到回答结束的 14 个步骤、每步每题平均耗时与 token、时间与 token 的构成、一道题逐轮拆解、工具与中间件调用（在线查看） |
| [docs/solutions/kunpeng-pool/index.html](https://sqlrush.github.io/gh_gs_k8s_agent/solutions/kunpeng-pool/) | 项目经理 / 售前 | **基于鲲鹏服务器的池化方案**（技术分析稿）：一次提问从浏览器到硬件的全链路与各段时间占比、模型推理 84.7% 的拆解与鲲鹏加速、全链路中鲲鹏参与的步骤与优势（数值待填）（在线查看） |

## v1.5.0 起：Fuyao 沙箱运行模式（预览）

后端新增第三种运行模式：每个用户的诊断环境、知识库管理员的导入环境各由 openFuyao 平台的一台微型虚拟机（沙箱）承载，
网关通过平台接口按需建、暂停、恢复、删除；集群里新增状态服务（替沙箱读写 NAS）与凭据代理服务（持有中间件令牌与模型密钥，密钥不进沙箱）；
另有资源大盘（运维管理员看沙箱容量、承载 Pod、组件与工号分布）。按需、固定池化两种模式照旧可用，改配置 `RUNTIME_MODE` 切换。

**预览**：平台的三项安全前提（envd 认证、沙箱出网限制、OpenSandbox 认证）落实之前，只用于测试环境（测试账号、测试库、模拟中间件），不接生产数据库与真实的中间件令牌。

| 交付物 | 内容 | 用在 |
|---|---|---|
| 5 个 Docker 镜像 + 发版台 | `ghcr.io/sqlrush/gaussdb-agent-{gateway,frontend,runtime,kb-import,kbstd}:agent-v1.5.2-oc1.18.27`（amd64 / arm64 双架构），发版台 `gaussdb-agent-release`；离线包里同样有 | K8s 集群：网关、前端、状态服务与凭据代理服务（runtime 镜像不同入口）、标准知识库安装 Job |
| 2 个沙箱模板镜像 | `ghcr.io/sqlrush/gaussdb-agent-{runtime-e2b,kb-import-e2b}:agent-v1.5.2-oc1.18.27`（本版只出 arm64）；离线包里是同内容的 OCI 包 `gaussdb-agent-v1.5.2-oc1.18.27-e2b-arm64.oci.tar`。从 GHCR 拉的要先导出成 OCI 包，做法见部署手册 §2.3 | 在平台的模板构建主机上注册成诊断、导入两种沙箱模板 |
| 模板注册脚本 | 本仓库 [`e2b/`](e2b/)（与离线包 `e2b/` 相同）：`e2b/scripts/build-e2b-template.py`、`e2b/scripts/oci-serve.py`、`e2b/docker/Dockerfile.e2b`（`--from-oci` 用上面的 OCI 包，本机起临时只读仓库供平台拉取） | 模板构建主机（root） |

| 文档 | 给谁 | 内容 |
|---|---|---|
| [docs/Fuyao沙箱部署手册.md](docs/Fuyao沙箱部署手册.md) | 运维 / 平台管理员 | **从零到上线的完整步骤**：部署前检查（平台与安全前提、网络连通矩阵、资源测算）、导入镜像、注册模板、建沙箱组、Secret 与配置、现场 overlay、上线验证、升级与回退、扩容、日常运维、排查、卸载 |
| [docs/Fuyao沙箱手册.md](docs/Fuyao沙箱手册.md) | 运维 / 后端 | Fuyao 模式的形态、生命周期（按需建、闲置暂停、保留期删除）、数据怎么进出 NAS、运维接口与排查 |

## 组成

| 组件 | 镜像 | 形态 | 说明 |
|---|---|---|---|
| 接入网关 | `gaussdb-agent-gateway` | Deployment，2 副本 | 认工号（上游 SSO 加的头 + 信任凭据）、按工号分配用户环境、反向代理对话与报告；控制 API `/admin/*` |
| 用户环境 | `gaussdb-agent-runtime` | 每人一个 Pod，或固定池化的槽位 | opencode 1.18.27 + 17 个诊断技能 + 知识库只读查询；凭据代理（中间件令牌、签名私钥、模型 Key 不进模型所在的容器） |
| 知识库导入环境 | `gaussdb-agent-kb-import` | 每个知识库管理员一个 | 知识库导入、入库前标准化、官方文档集（CHM）导入；内网网页剪藏（书签 → 剪藏页 → 导入环境）、扫描件 PDF 看图转录、PPT 导入 |
| 标准知识库 | `gaussdb-agent-kbstd`（我方发布） | 部署时跑一次的 Job | GaussDB 官方文档集，`kubectl apply` 后自动装进知识库（同版本跳过、不碰贵方知识） |
| 发版台（可选） | `gaussdb-agent-release`（我方发布） | 跳板机上的容器，不进 K8s | 首次部署、升级、回滚：发布前扫描资源、变更预览由运维确认、固定池化按槽位在线替换、冒烟验证；见《发版台运维手册》 |
| 大盘前端 | `gaussdb-agent-frontend` | Deployment | 诊断大盘、资源大盘的静态页面（`frontend/`），经网关 `/dash/*` 访问；门户页面按《前端对接接口说明》接入 |
| NAS | — | 一个 RWX 卷 | 唯一的状态所在：`users/<工号>/`（会话、报告）、`kb/`（知识库与索引 `kb/index/store.sqlite`） |

## 目录

```
agent/       技能代码:common/ 公共模块(含 timing/ 交互耗时报告)、skills/ 技能、grmp_middleware/ 中间件客户端与模拟服务、
             scripts/registry/ 白名单脚本定义、tests/ 单测
docker/      Dockerfile(runtime / kb-import / gateway / kbstd / release / e2b 沙箱模板)、容器启动脚本、凭据代理、固定池化的管家与槽位程序
frontend/    大盘前端(诊断大盘、资源大盘)的静态页面
gateway/     接入网关
k8s/         base/ 部署清单;fuyao/ Fuyao 沙箱模式;templates/ 网关建 Pod 的模板;overlays/ 现场覆盖;release/ 发版台;tools/ 现场工具;local/ 本地测试集群
release/     发版台(首次部署、升级、回滚)与离线包元数据
scripts/     构建、离线打包、模板注册(build-e2b-template.py、oci-serve.py)、测试集群与端到端脚本
e2b/         Fuyao 沙箱模板材料(与离线包 e2b/ 相同)
docs/        面向客户的手册、白名单(delivery/)、接口定义(api/)、各版部署页
acceptance/  智能体版本验收:题库、夹具、跑题评分出报告
delivery/    转化脚本:从发布标签生成 diag / kbimport 两个技能包分支
```

`frontend/` 自 v1.5.0 起是我方大盘前端（`gaussdb-agent-frontend` 镜像）的源码，随发布标签同步；前端方的门户页面按《前端对接接口说明》对接，如需放在本仓库，另建目录并在下表登记。

## 分工与协作约定

| 范围 | 负责方 | 目录 |
|---|---|---|
| 后端：网关、用户镜像、技能、大盘前端、部署清单、发版台、构建与打包脚本、沙箱模板材料、验收题库 | 我方（开发仓库同步） | `agent/`、`docker/`、`frontend/`、`gateway/`、`k8s/`、`release/`、`scripts/`、`e2b/`、`acceptance/`、`delivery/` |
| 前端：门户页面 | 前端方 | 另建目录并登记 |
| 接口：对话 / 报告接口、事件、链接格式、报告字段契约 | 双方共同 | `docs/前端对接接口说明.md`、`docs/api/` |

- **接口变更走契约**：后端改报告字段、前端要用新字段，都先改 `docs/api/reports-contract.json` 与接口说明，再改代码，三者在同一个提交里；
  契约测试 `agent/tests/test_frontend_contract_units.py` 会拦住不一致。不兼容的改动要升接口版本（接口说明第 10 章）。
- **提交信息**：`<类型>(<范围>): <中文说明>`，类型用 `feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `ci`。
- **合入前测试全绿**（见下）。前端方的测试放在 `frontend/` 下，自带运行方式。
- **本仓库公开**：不提交任何凭据（模型 Key、中间件令牌、SM2 私钥、信任凭据）、客户名称与地址、客户文档原文。
  这些都只在部署时以 Secret / ConfigMap 注入。

## 测试

在仓库根目录（Python 3.10 以上）：

```bash
python3 -m venv .venv && source .venv/bin/activate      # 必须激活:部分测试会直接调用 PATH 里的 python3
pip install pytest PyYAML cryptography psycopg2-binary sqlite-vec pillow pycryptodomex
python -m pytest -q
```

七组测试：`agent/tests/`（技能与公共模块）、`docker/tests/`（容器程序）、`gateway/tests/`（网关）、`release/tests/`（发版台）、`scripts/k8s/tests/`（部署脚本与清单守卫）、`delivery/tests/`（转化脚本）、`acceptance/tests/`（验收题库）。
需要真实数据库的用例（标记 `live`）没配连接时自动跳过。

## 许可

技能代码见 `agent/LICENSE`（Apache License 2.0）与 `agent/NOTICE`。
