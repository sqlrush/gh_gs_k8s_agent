# gh_gs_k8s_agent

GaussDB 数据库智能体的**后端**：把 opencode 与 GaussDB / openGauss 诊断技能放进 Kubernetes。
每个用户一个隔离的环境（按需建 Pod，或固定池化的槽位），状态全部在 NAS 上；接入网关负责认身份、分配环境、反向代理。

本仓库是**联合项目**：我方负责后端，前端方负责前端，双方（含各自的编码智能体）在同一个仓库里协作。

## v1.4.7 起：交付分支

代码只在测试仓库 [gh_agent_k8s](https://github.com/sqlrush/gh_agent_k8s) 维护（完整代码 + 行外自测用的模拟组件）。交付给行内的是本仓库的两个分支，
由测试仓库的转化脚本 `delivery/transform.py` 从发布标签自动生成——**不要在这两个分支上手工修改**（转化脚本发现手改会停下，需要的改动挪回测试仓库再生成）：

| 分支 / 标签 | 内容 | 用在 |
|---|---|---|
| [`diag`](https://github.com/sqlrush/gh_gs_k8s_agent/tree/diag) / `diag-v1.4.13` | 诊断技能包：`AGENTS.md`、`common/`、17 个诊断技能 + `gaussdb-kb`、`scripts/registry/`（白名单脚本定义）、`whitelist/`（132 条白名单 DML、历次增量与清单；从 v1.4.11 / v1.4.12 升级导 `scripts-delta-20261112.sql`（新增 2 条退路脚本；原来 8 条两段 SQL 的脚本不再使用，可以保留），从 v1.4.10 升级先导 `scripts-delta-20261111.sql`（新增 1、更新 71 条执行库）再导 20261112，从 v1.4.7 ~ v1.4.9 升级依次导 `scripts-delta-20261108.sql`、20261111、20261112，从 v1.4.3 ~ v1.4.6 升级先导 `scripts-delta-20261105.sql`、再依次导 20261108、20261111 与 20261112；不确定导到哪一份时，把白名单导出交给我方核对）、`rootspec/search_docs.py`（根规范检索脚本）、`补充说明.md` | 诊断环境 |
| [`kbimport`](https://github.com/sqlrush/gh_gs_k8s_agent/tree/kbimport) / `kbimport-v1.4.13` | 知识库导入技能包：`AGENTS.md`、`common/`、`gaussdb-kb-import`、`gaussdb-kb-init`、`gaussdb-kb`、`rootspec/search_docs.py`、`补充说明.md` | 知识库导入环境 |

v1.4.7 起诊断站在应用 DBA / 运维 DBA 的角度（指标异常追到引起它的 SQL、会话、应用、表；处置先改应用、再做运维操作，参数只在典型场景给）。v1.4.8 修复 api 模式登录：中间件按实例 IP 固定连一个库、不按库名切换，原来填业务库名一律登录不了；v1.4.9 起库名与 schema 的限制全部放开（库名核对不拦登录；同名表跨 schema 取一个照常跑），中间件固定连 postgres 时业务库的 SQL 按执行历史调优。v1.4.11 按行方切库方案调用中间件时带 `dbName`：登录时给了业务库（`--database <业务库>`），表、索引、执行计划、统计信息、存储过程就在该业务库上取，SQL 执行历史与 WDR 快照仍在 postgres；白名单要导本版增量，登录看到「已直连」即生效。v1.4.12 起行方白名单没导入该增量时也不报错（取数在 postgres 上），查不到慢存储过程时说清是没切到业务库、参数关着还是还没有调用记录。v1.4.13 是上生产前的稳定版：界面上不出现报错原文（中间件地址、接口路径、实例 ID 不显示，原文进本机诊断日志、报错带详情编号），某一项取不到时写「这一部分无法查询」、其余照常给出；不再发两段 SQL；视图没数据时换 SQL 取。每个分支根目录的 `补充说明.md` 写着：版本对应、包内容与技能版本、依赖（Python 包与系统程序）、环境变量（必填 / 默认值）、SQL 白名单与上一版的差异、
根规范目录约定、变更摘要。

```bash
git clone -b diag-v1.4.13 https://github.com/sqlrush/gh_gs_k8s_agent.git gaussdb-diag          # 诊断技能包
git clone -b kbimport-v1.4.13 https://github.com/sqlrush/gh_gs_k8s_agent.git gaussdb-kbimport  # 知识库导入技能包
```

**拉下来之后要装进正在用的技能目录**，只 clone 不会生效。以技能目录 `/workspace/.opencode/skills` 为例（换成你们环境里的 `$GSDB_SKILLS_DIR`）：

```bash
SK=/workspace/.opencode/skills
cp $SK/common/config.yaml /tmp/config.yaml.bak          # 先备份本地配置(中间件地址等),包里的同名文件只是模板
cp -r gaussdb-diag/common/. $SK/common/
cp -r gaussdb-diag/scripts/. $SK/scripts/
cp -r gaussdb-diag/skills/* $SK/
cp /tmp/config.yaml.bak $SK/common/config.yaml          # 还原本地配置
cat $SK/common/VERSION                                 # v1.4.13 显示 agent-v1.4.13
```

`AGENTS.md` 放进 opencode 的配置目录。换完**新开对话**（旧对话里读进来的还是旧版技能说明）。

装完后登录任一实例，在对话里说「做一次部署自检」（或运行 `python3 $SK/gaussdb-login/scripts/selfcheck.py --session <句柄>`）：它核对技能包版本、白名单是否导全、关键脚本能否执行（含执行账号权限）、数据库监控开关，以及（v1.4.11 起）本会话是否切到了业务库、行方白名单里各脚本的执行库与本版是否一致，逐项写明结论和找谁。

`main` 上的后端代码停在 `agent-v1.4.6`，不再更新；v1.4.6 及以前的部署上线方案照旧按各自的标签使用。

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
| [docs/delivery/白名单脚本清单.md](docs/delivery/白名单脚本清单.md) | 中间件团队 | 128 条诊断脚本的用途、参数与 SQL 全文；全量 `scripts.sql` 与 5 份升级增量都在 `docs/delivery/` |
| [docs/从源码构建到K8s运行-完整手册.md](docs/从源码构建到K8s运行-完整手册.md) | 实施 / 运维 / 开发 | 从拉代码、构建 3 个镜像、推仓库，到部署进 K8s、开通工号、对话里跑通健康检查；可选的固定池化。每步带实测输出 |
| [docs/前端对接接口说明.md](docs/前端对接接口说明.md) | 前端 | 接入后端的全部接口（v1.1）：身份与网关行为、对话接口与事件流、报告接口、大盘链接格式、回答正文的渲染与版式、联调清单 |
| [docs/智能体验收方案.md](docs/智能体验收方案.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/acceptance/)） | 后端 / 项目经理 | **每次升级智能体怎么验收**：11 个方面 48 道题、每题的评分标准（版式、工具、要点、处置、引用、禁止项）、三层评分、失分归因到工具 / 提示词 / 模型、发版门槛；题库在 [acceptance/](acceptance/) |
| [docs/代码结构说明.md](docs/代码结构说明.md) | 后端 / 前端 / 双方的编码智能体 | 逐个目录说明每个文件的作用、一个请求在代码里怎么走、改什么去哪里（有守卫测试，加删文件要同步） |
| [docs/api/](docs/api/) | 前端 / 后端 | 机器可读的接口定义：`opencode-1.18.27-openapi.json`（对话接口全集）、`reports-contract.json`（报告字段契约，后端单测逐条核对） |
| [docs/delivery/回答模板化-交付说明.md](docs/delivery/回答模板化-交付说明.md) | 平台 / 中间件 / 前端 | 回答模板化（2026-10-26）要做的事：白名单增量 3 条、镜像升级、前端第二种颜色、装后冒烟 |
| [docs/reports/answer-template-comparison-20261026.html](https://sqlrush.github.io/gh_gs_k8s_agent/reports/answer-template-comparison-20261026.html) | 双方 | 回答模板化前后对比：同一组 11 道题在测试环境改造前后各问一次，格式检查、耗时、生成量与原文并排（在线查看） |
| [docs/reports/qa-flow-v1.4.10-20261109.html](https://sqlrush.github.io/gh_gs_k8s_agent/reports/qa-flow-v1.4.10-20261109.html) | 双方 / 项目经理 | **一次提问的全流程：步骤、耗时与 token**（agent-v1.4.10 测试环境实测，9 道代表性题目各问 2 次）：从提问进入到回答结束的 14 个步骤、每步每题平均耗时与 token、时间与 token 的构成、一道题逐轮拆解、工具与中间件调用（在线查看） |
| [docs/solutions/kunpeng-pool/index.html](https://sqlrush.github.io/gh_gs_k8s_agent/solutions/kunpeng-pool/) | 项目经理 / 售前 | **基于鲲鹏服务器的池化方案**（技术分析稿）：一次提问从浏览器到硬件的全链路与各段时间占比、模型推理 84.7% 的拆解与鲲鹏加速、全链路中鲲鹏参与的步骤与优势（数值待填）（在线查看） |

## 组成

| 组件 | 镜像 | 形态 | 说明 |
|---|---|---|---|
| 接入网关 | `gaussdb-agent-gateway` | Deployment，2 副本 | 认工号（上游 SSO 加的头 + 信任凭据）、按工号分配用户环境、反向代理对话与报告；控制 API `/admin/*` |
| 用户环境 | `gaussdb-agent-runtime` | 每人一个 Pod，或固定池化的槽位 | opencode 1.18.27 + 17 个诊断技能 + 知识库只读查询；凭据代理（中间件令牌、签名私钥、模型 Key 不进模型所在的容器） |
| 知识库导入环境 | `gaussdb-agent-kb-import` | 每个知识库管理员一个 | 知识库导入、入库前标准化、官方文档集（CHM）导入；内网网页剪藏（书签 → 剪藏页 → 导入环境）、扫描件 PDF 看图转录、PPT 导入 |
| 标准知识库 | `gaussdb-agent-kbstd`（我方发布） | 部署时跑一次的 Job | GaussDB 官方文档集，`kubectl apply` 后自动装进知识库（同版本跳过、不碰贵方知识） |
| 发版台（可选） | `gaussdb-agent-release`（我方发布） | 跳板机上的容器，不进 K8s | 首次部署、升级、回滚：发布前扫描资源、变更预览由运维确认、固定池化按槽位在线替换、冒烟验证；见《发版台运维手册》 |
| 前端 | 前端方自建 | 可部署进本集群（`k8s/base/frontend.example.yaml`），也可在贵方门户 | 按《前端对接接口说明》接入 |
| NAS | — | 一个 RWX 卷 | 唯一的状态所在：`users/<工号>/`（会话、报告）、`kb/`（知识库与索引 `kb/index/store.sqlite`） |

## 目录

```
agent/       技能代码:common/ 公共模块、skills/ 20 个技能(runtime 装 18 个,kb-import 装 3 个)、grmp_middleware/ 中间件客户端与模拟服务、tests/ 单测
docker/      Dockerfile(base → runtime / kb-import)、Dockerfile.gateway、容器启动脚本、凭据代理、固定池化的管家与槽位程序、报告端口
gateway/     接入网关
k8s/         base/ 部署清单;templates/ 网关按工号建 Pod 的模板;tools/ 现场工具(runtime-doctor、sso-sim、render-pool);local/ 本地测试集群覆盖
scripts/     构建、离线打包、大盘样例数据(dash-samples.py)、测试集群与端到端脚本
docs/        构建部署手册、前端对接接口说明、代码结构说明、智能体验收方案、api/ 机器可读的接口定义
acceptance/  智能体版本验收:bank/v1/ 48 题评分标准、fixtures/ 夹具、run.py 跑题评分出报告、check_bank.py 离线校验
```

前端代码约定放在 `frontend/`（由前端方建立与维护）。

## 分工与协作约定

| 范围 | 负责方 | 目录 |
|---|---|---|
| 后端：网关、用户镜像、技能、部署清单、构建与打包脚本、验收题库 | 我方 | `agent/`、`docker/`、`gateway/`、`k8s/`、`scripts/`、`acceptance/` |
| 前端：页面、大盘、前端镜像 | 前端方 | `frontend/` |
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

五组测试：`agent/tests/`（技能与公共模块）、`docker/tests/`（容器程序）、`gateway/tests/`（网关）、`scripts/k8s/tests/`（部署脚本与清单守卫）、`acceptance/tests/`（验收题库）。
需要真实数据库的用例（标记 `live`）没配连接时自动跳过。

## 许可

技能代码见 `agent/LICENSE`（Apache License 2.0）与 `agent/NOTICE`。
