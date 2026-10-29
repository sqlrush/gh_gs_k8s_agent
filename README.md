# gh_gs_k8s_agent

GaussDB 数据库智能体的**后端**：把 opencode 与 GaussDB / openGauss 诊断技能放进 Kubernetes。
每个用户一个隔离的环境（按需建 Pod，或固定池化的槽位），状态全部在 NAS 上；接入网关负责认身份、分配环境、反向代理。

本仓库是**联合项目**：我方负责后端，前端方负责前端，双方（含各自的编码智能体）在同一个仓库里协作。

| 文档 | 给谁 | 内容 |
|---|---|---|
| [docs/部署上线方案-v1.4.2.md](docs/部署上线方案-v1.4.2.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.2/)） | 项目经理 / 各方团队 | **从拉代码到部署上线的完整方案**（agent-v1.4.2）：系统组成与安全设计、各方分工与检查单、构建、白名单导入、部署、开通、验收、固定池化、知识库、统一认证与前端、内网网页剪藏、官方文档集随部署自动装上、从 v1.4.1 / v1.4.0 / v1.3.x 升级、回滚、运维，附全部 126 条白名单脚本 |
| [docs/部署上线方案-v1.4.1.md](docs/部署上线方案-v1.4.1.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.1/)） | 留档 | 上一版（agent-v1.4.1）的部署上线方案 |
| [docs/部署上线方案-v1.4.0.md](docs/部署上线方案-v1.4.0.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/deploy/v1.4.0/)） | 留档 | 上一版（agent-v1.4.0）的部署上线方案 |
| [docs/delivery/白名单脚本清单.md](docs/delivery/白名单脚本清单.md) | 中间件团队 | 126 条诊断脚本的用途、参数与 SQL 全文；全量 `scripts.sql` 与 4 份升级增量都在 `docs/delivery/` |
| [docs/从源码构建到K8s运行-完整手册.md](docs/从源码构建到K8s运行-完整手册.md) | 实施 / 运维 / 开发 | 从拉代码、构建 3 个镜像、推仓库，到部署进 K8s、开通工号、对话里跑通健康检查；可选的固定池化。每步带实测输出 |
| [docs/前端对接接口说明.md](docs/前端对接接口说明.md) | 前端 | 接入后端的全部接口（v1.1）：身份与网关行为、对话接口与事件流、报告接口、大盘链接格式、回答正文的渲染与版式、联调清单 |
| [docs/智能体验收方案.md](docs/智能体验收方案.md)（[在线查看](https://sqlrush.github.io/gh_gs_k8s_agent/acceptance/)） | 后端 / 项目经理 | **每次升级智能体怎么验收**：11 个方面 48 道题、每题的评分标准（版式、工具、要点、处置、引用、禁止项）、三层评分、失分归因到工具 / 提示词 / 模型、发版门槛；题库在 [acceptance/](acceptance/) |
| [docs/代码结构说明.md](docs/代码结构说明.md) | 后端 / 前端 / 双方的编码智能体 | 逐个目录说明每个文件的作用、一个请求在代码里怎么走、改什么去哪里（有守卫测试，加删文件要同步） |
| [docs/api/](docs/api/) | 前端 / 后端 | 机器可读的接口定义：`opencode-1.18.27-openapi.json`（对话接口全集）、`reports-contract.json`（报告字段契约，后端单测逐条核对） |
| [docs/delivery/回答模板化-交付说明.md](docs/delivery/回答模板化-交付说明.md) | 平台 / 中间件 / 前端 | 回答模板化（2026-10-26）要做的事：白名单增量 3 条、镜像升级、前端第二种颜色、装后冒烟 |
| [docs/reports/answer-template-comparison-20261026.html](https://sqlrush.github.io/gh_gs_k8s_agent/reports/answer-template-comparison-20261026.html) | 双方 | 回答模板化前后对比：同一组 11 道题在测试环境改造前后各问一次，格式检查、耗时、生成量与原文并排（在线查看） |
| [docs/solutions/kunpeng-pool/index.html](https://sqlrush.github.io/gh_gs_k8s_agent/solutions/kunpeng-pool/) | 项目经理 / 售前 | **基于鲲鹏服务器的池化方案**（技术分析稿）：一次提问从浏览器到硬件的全链路与各段时间占比、模型推理 84.7% 的拆解与鲲鹏加速、全链路中鲲鹏参与的步骤与优势（数值待填）（在线查看） |

## 组成

| 组件 | 镜像 | 形态 | 说明 |
|---|---|---|---|
| 接入网关 | `gaussdb-agent-gateway` | Deployment，2 副本 | 认工号（上游 SSO 加的头 + 信任凭据）、按工号分配用户环境、反向代理对话与报告；控制 API `/admin/*` |
| 用户环境 | `gaussdb-agent-runtime` | 每人一个 Pod，或固定池化的槽位 | opencode 1.18.27 + 17 个诊断技能 + 知识库只读查询；凭据代理（中间件令牌、签名私钥、模型 Key 不进模型所在的容器） |
| 知识库导入环境 | `gaussdb-agent-kb-import` | 每个知识库管理员一个 | 知识库导入、入库前标准化、官方文档集（CHM）导入；内网网页剪藏（书签 → 剪藏页 → 导入环境）、扫描件 PDF 看图转录、PPT 导入 |
| 标准知识库 | `gaussdb-agent-kbstd`（我方发布） | 部署时跑一次的 Job | GaussDB 官方文档集，`kubectl apply` 后自动装进知识库（同版本跳过、不碰贵方知识） |
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
