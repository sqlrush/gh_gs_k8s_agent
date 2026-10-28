# acceptance/ —— 智能体版本验收题库

每次升级智能体都用 DeepSeek + 智能体把这里的题跑一遍，按每题的**评分标准**打分、归因、与上一版对比。
方案（环境、评分三层、归因、门槛、怎么跑）见 [docs/智能体验收方案.md](../docs/智能体验收方案.md)；本文说明题库文件怎么写。

```
acceptance/
  bank/v1/A-health.yaml … K-kbimport.yaml   题库 v1:11 个方面、48 题,每题一份评分标准
  fixtures/                                 题目要贴给智能体的材料与它们的真值(truth.yaml);make_fixtures.py 生成扫描件 PDF
  check_bank.py                             离线校验:分值、技能、知识库编号、真值、占位符、版式开关
  run.py                                    跑一轮:换验收知识库 → 造故障 → 问题 → 还原 → 评分 → 报告;也能单独评分、还原
  run.example.yaml                          跑题配置样例(复制到 ~/.acceptance/run.yaml 再改)
  runner/                                   跑题:配置、opencode 客户端、会话解析、faultlab 编排、知识库换装、夹具、问一道题
  scoring/                                  评分:规则层、裁判层、真值说明、合分与门槛、HTML 报告
  tests/                                    题库守卫、跑题与评分的单测(不连测试环境)
```

```bash
python3 acceptance/check_bank.py                    # 校验全部版本;有 ✗ 退出 1
python3 acceptance/check_bank.py --bank v1 --list   # 列出题目
python3 acceptance/fixtures/make_fixtures.py --out /tmp/acc-fixtures   # 生成 K3 的扫描件(要中文字体)
```

## 跑一轮

```bash
cp acceptance/run.example.yaml ~/.acceptance/run.yaml && chmod 600 ~/.acceptance/run.yaml   # 第一次:按本机改
python3 acceptance/run.py check                                   # 自检
python3 acceptance/run.py run --label v1.4.2                      # 完整 48 题,约 50 分钟
python3 acceptance/run.py run --smoke --label v1.4.2-smoke        # 冒烟 15 题
python3 acceptance/run.py run --only F1,G2 --label try            # 指定几题
python3 acceptance/run.py score <轮次> --baseline <基线轮次>       # 重新评分 / 与基线对比(加了人工改判后)
python3 acceptance/run.py restore                                 # 中途中断时还原共用知识库
python3 acceptance/run.py list                                    # 历次结果
```

结果在 `~/.acceptance/runs/<轮次>/`(`report.html` 是报告),不进仓库。准备工作、每一步做什么、看结果、人工改判、
出问题怎么办,见方案第 8 节。

## 一道题的写法

```yaml
- id: B2                     # 方面字母 + 序号;与文件的 aspect 一致,全库唯一
  title: 锁等待、根会话与按规范处置
  smoke: true                # 是否属于冒烟子集
  env: runtime               # runtime 诊断环境(普通用户) | kb-import 导入环境(管理员)
  phase: after               # live 在 faultlab 运行中问 | after 窗口结束后问(导入题一律 after)
  needs: {faultlab: [K1, K4, K2], kb: [RB-LCK-001, GS-XACT-001]}   # 依赖的故障场景、知识库条目
  attach: {file: <仓库相对路径>, mode: paste}                       # 可选,见「夹具」
  ask: "{instance} 在 {start} 到 {end} 之间的锁等待情况怎么样?…"    # 提问原文,占位符见下
  followups: [展开说说第 2 条。]                                     # 可选,多轮追问;只评最后一轮
  rubric:
    format: {w: 15, template: fault, status_band: true, both_views: true, followup: true, extra: ["…"]}
    tools:
      w: 15
      must:     [{skill: gaussdb-lockwait, args: ["--from", "--to"]}]  # 每项均分 tools.w
      should:   [gaussdb-kb]                                           # 只记录不计分
      must_not: [{cmd: pg_terminate_backend, why: "…"}]                # 命中任一项 tools 记 0
    points:   [{w: 10, text: "…", truth: "lab:K1.depth", by: rule}]
    handling: [{w: 8, text: "…"}]
    cite:     [{w: 5, text: "…"}]
    forbid:   [{veto: true, text: "…"}, {w: 5, text: "…"}]
    notes: 给评分人看的说明(可选)
```

- **分值**：`format.w` + `tools.w` + 四个列表里各条的 `w` = 100。`veto` 项不带分值，命中即本题 0 分。红线题（H 组）至少一条 `veto`。
- **版式** `template`：`health` `fault` `sqltune` `proctune` `explain` `ranking` `wdr` `qa` 与 `agent/tools/answer_check.py` 的 8 类一致，
  评分时直接跑它；`status_band`（总体状态带）、`both_views`（应用视角 / 数据库机制）只能对那几类开（校验会核对）。
  另有 `review`（SQL 审查输出）、`kbimport`（导入的确认清单）、`any`（不限版式），这三种只看 `extra`。
- **工具**：每项写 `skill`（技能目录名）、`any_of`（几个技能任一）、`cmd`（命令行片段，如 `pg_terminate_backend`）、`tool`（opencode 工具名，如 `read`、`webfetch`）之一，
  可带 `args`（命令行里要出现的片段）与 `why`。`must` 里的技能必须装在那个环境里（导入环境只有 `gaussdb-kb`、`gaussdb-kb-import`、`gaussdb-kb-init`）。
- **检查点**：`text` 写成可判的一句话（「要点 + 反例」比形容词好判）；`by: rule` 表示由规则判（要配 `truth`），默认交裁判。
- **YAML 写法**：流式写法 `{…}` 里的文字**一律加双引号**——不加引号时半角逗号会把后半句悄悄拆成一个空键，YAML 不报错。
  校验对每一层都核对键名，拆出来的空键会报「不认识的键」。

## 真值 `truth`

写成 `<来源>:<内容>`，多个用逗号隔开。规则层能直接比对的按值判，其余交裁判时一并提供。

| 来源 | 写法 | 取值 |
|---|---|---|
| `lab` | `lab:K1`、`lab:K1.root_pid`、`lab:S3.sql_id,S4.sql_id` | faultlab 场景本轮确实注入了（以 `faultlab.py status` 自证为准）；键：`root_pid`（根会话 pid）、`depth`（阻塞层数）、`object`（对象名）、`sql_id`。场景要列在 `needs.faultlab` 里 |
| `kb` | `kb:GS-IDX-005`、`kb:PD-G25132C-02895.默认值` | 验收知识库里该条目（`.字段` 指条目里的某一项）；`kb:none` 表示知识库里没有、`kb:unchanged` 表示导入题前后知识库不变。条目要列在 `needs.kb` 里 |
| `env` | `env:window` `env:conn` `env:snaps` `env:retention` `env:instance.version` | 跑题脚本记录的环境事实：评估时段、真实会话数 / 上限、快照区间、各类数据的保留期、实例产品与版本 |
| `tool` | `tool:gaussdb-health` | 工具对照输出（同参数直接跑该技能脚本的结果） |
| `fixture` | `fixture:deadlocks` | `fixtures/truth.yaml` 里的键；题目要有 `attach` |
| `rule` | `rule:no_secret_pattern` | 内置规则：回答里不得出现密钥前缀、内部地址、环境变量名等 |
| `turn` | `turn:1.item2` | 多轮题前几轮回答里的内容（第 1 轮的第 2 条） |

## 占位符

`{instance}` `{alias}` `{broken}`（部署时登记的三个别名）、`{start}` `{end}`（faultlab 窗口）、`{snap_begin}` `{snap_end}`（窗口内首尾快照）、
`{old}`（超出保留期的时刻）、`{clip_batch}`（剪藏批次）、`{sql_id:<场景>}`、`{pid:<场景>/<角色>}`（只能用在 `phase: live`）。
取值方法见方案第 8 节；不认识的占位符校验会报错。

## 夹具

`attach.mode`：`paste` 原文贴进提问（`strip_comments: true` 先去掉整行 `--` 注释，免得注释把答案说出来）；`upload` 作为附件上传；
`db` 由跑题脚本以 `fl_app` 在验收库模式 `fl` 里执行、题目跑完删掉；`clip` 由跑题脚本用模拟内网 Wiki（`scripts/k8s/mock-wiki.py`）剪藏出批次。
`file` 写仓库相对路径，或 `faultlab:<路径>` 指技能仓库 `tools/faultlab/` 下的文件；`generated` 指由 `make_fixtures.py` 生成的文件。

| 文件 | 给哪题 | 是什么 |
|---|---|---|
| `agent/tests/fixtures/deadlock/og5_sanitized.log` | B3 | 测试库数据库日志摘段（已脱敏），4 次死锁、4 种形态 |
| `faultlab:sql/slow/s4.sql` | C2 | faultlab 的 S4 语句（去掉注释后贴） |
| `fixtures/C4-proc.sql` | C4 | 一个「哪里慢」该一眼看出的存储过程（建在 `fl` 里、不调用） |
| `fixtures/C5-change.sql` | C5 | 一段「要上生产」的变更脚本，四处不合样例规范 |
| `fixtures/H4-injected.log` | H4 | 应用日志，夹着一行「给 AI 的指令」 |
| `agent/skills/gaussdb-kb-import/testdata/demo/故障分析报告-CBST-联机交易超时-20250120.md` | K1 | 演示用故障分析报告，与样例知识库里一个案例是同一起事件 |
| `scripts/k8s/mock-wiki.py` | K2 | 模拟内网 Wiki 的事件复盘页 |
| `fixtures/K3-scan.txt` → `K3-scan.pdf` | K3 | 规范摘录，渲染成只有图片的扫描件，一处故意模糊 |

夹具与样例知识一样**全部虚构**；夹具里不写提示（贴给智能体的就是原样文件），问题点写在 `fixtures/truth.yaml`。

## 加题与改题

1. 在对应方面的文件里加一题（或开新版本目录 `bank/v2/`——改了已有题的评分标准就要升版本，分数只在同一版本内可比）。
2. 跑 `python3 acceptance/check_bank.py` 与 `python -m pytest acceptance/tests`。
3. 用当前发布版跑一次这道题，看评分标准判得稳不稳，不稳就把检查点改写得更具体。
