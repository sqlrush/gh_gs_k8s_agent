# delivery/:测试仓库 → 交付分支的转化脚本

本仓库(gh_agent_k8s)是**测试仓库**:完整代码 + 行外自测用的模拟组件(模拟中间件、模拟 SSO、假 opencode、故障演练、测试与夹具)。
代码只在这里改。交付给行内的是联合仓库 **gh_gs_k8s_agent** 上的两个**交付分支**,由本目录的转化脚本从发布标签自动生成:

| 交付分支 / 标签 | 包 | 内容 | 对应镜像 |
|---|---|---|---|
| `diag` / `diag-vX` | 诊断技能包 | `AGENTS.md`、`common/`、17 个诊断技能 + `gaussdb-kb`、`scripts/registry/`(白名单脚本定义)、`whitelist/`(给中间件团队的 DML)、`rootspec/search_docs.py`(根规范检索脚本)、`补充说明.md` | runtime |
| `kbimport` / `kbimport-vX` | 知识库导入技能包 | `AGENTS.md`(由 `AGENTS-kb-import.md` 改名)、`common/`、`gaussdb-kb-import` + `gaussdb-kb-init` + `gaussdb-kb`、`rootspec/search_docs.py`、`补充说明.md` | kb-import |

`补充说明.md` 每版自动生成:版本对应、包内容与技能版本、依赖、环境变量(必填 / 默认值)、SQL 白名单与上一版的差异、根规范目录约定、变更摘要。
`.delivery.json` 记录来源标签、提交与内容摘要,转化脚本靠它判断交付分支有没有被手改。

## 用法

只需要 git + Python 3.9+(不装第三方库也能跑;装了 PyYAML 等,注入核对与导入检查更完整,缺的会打印「提示」)。

```bash
# 预演:生成、自检、打印与交付分支的差异,不写任何东西
python3 delivery/transform.py --ref agent-v1.4.7 --package diag --target ~/gh_gs_k8s_agent --dry-run
# 正式:在交付仓库本地的 diag 分支上提交并打标签 diag-v1.4.7;推送命令会打印出来,脚本自己不推
python3 delivery/transform.py --ref agent-v1.4.7 --package diag --target ~/gh_gs_k8s_agent --tag
python3 delivery/transform.py --ref agent-v1.4.7 --package kbimport --target ~/gh_gs_k8s_agent --tag
# 只生成到一个空目录
python3 delivery/transform.py --ref agent-v1.4.7 --package kbimport --out /tmp/kbimport
```

退出码:0 成功(含「内容没变,不提交」);2 预期内的停止(自检没过、交付分支被手改、参数不对),原因打印在标准错误。

## 它做了什么

1. `git archive` 取标签里的 `agent/`、`delivery/`、`docs/delivery/`、`docker/search_docs.py`(清单的 `EXTRACT`)——不读工作区;清单与脚本也用标签里那一版,同一标签任何时候生成的结果逐字相同。
2. 在完整源码上核对红线、回答模板、知识库契约的注入与正文一致(交付包里不带这些工具,只能裁剪前核对)。
3. 按 `manifest.py` **选**文件(不是排除法):技能按名单(去掉 `testdata/`、`tests/`),common 按归属,白名单脚本定义只带技能实际调用的,另带清单 `extra` 里的根规范检索脚本。
4. 自检(`checks.py`),任何一项不过就停:
   - 顶层条目、技能、文件与清单逐个对上;
   - 引用的 `common.*` 都在包里(静态);每个技能脚本在交付包里真 import 一遍(动态,`python -I`);
   - 没有测试环境路径、行名(只存哈希比对,仓库里不写行名本身)、密钥、内网 IP、对模拟组件的调用;
   - 诊断包的白名单脚本定义与 `whitelist/scripts.sql` 逐条一致;
   - 没有字节码缓存。
5. 生成 `补充说明.md` 与 `.delivery.json`,在交付仓库的临时工作树里提交(交付分支不存在就建孤儿分支)。

交付分支**只允许本脚本写**:分支内容与它记录的摘要对不上(有人手改过)就停下,不覆盖——需要的改动挪回测试仓库再生成。

## 改代码时要同步改清单

`manifest.py` 是唯一要手工维护的地方,守卫测试(`delivery/tests/`)会指出漏了什么:

| 改动 | 要做的 |
|---|---|
| 新增 / 删除 common 文件 | 在 `COMMON` 里登记归属(`shared` 两包都带 / `diag` / `kbimport`);shared 不能依赖只属于某个包的模块 |
| 新增技能 | 加进 `DIAG_SKILLS` 或 `KBIMPORT_SKILLS` |
| 代码里新读一个环境变量 | 在 `ENV` 里登记(哪些包读、必填、说明 / 默认值) |
| 新增 Python 依赖或系统程序 | `PY_DEPS` / `SYS_DEPS`(与 `docker/Dockerfile`、`agent/requirements.txt` 核对) |
| 升级 opencode | `OPENCODE_VERSION` 与 Dockerfile 一致 |

```bash
python3 -m pytest -q -p no:cacheprovider delivery/tests     # 在仓库根跑(用根 pytest.ini)
```
