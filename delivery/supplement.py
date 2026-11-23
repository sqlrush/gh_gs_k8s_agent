"""生成交付包的「补充说明.md」:版本对应、包内容、依赖、环境变量、SQL 白名单、根规范约定、变更摘要。

内容全部来自交付清单(manifest.py)、交付包本身与测试仓库的提交记录;不写当前时间,同一来源提交生成的结果逐字相同。
"""
from __future__ import annotations

import pathlib
import re
from typing import Dict, List, Optional, Sequence


def skill_version(skill_md: pathlib.Path) -> str:
    m = re.search(r"^version:\s*([^\s#]+)", skill_md.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "-"


def _table(head: Sequence[str], rows: List[Sequence[str]]) -> List[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return out


def _image_tag(ref: str, manifest) -> str:
    return "%s-oc%s" % (ref, manifest.OPENCODE_VERSION) if ref.startswith("agent-v") else "(来源不是发布标签)"


def render(manifest, package: str, root: pathlib.Path, source: Dict[str, str], version: str,
           whitelist_diff: Optional[Dict[str, List[str]]], changes: Optional[List[str]],
           prev: Optional[Dict[str, str]]) -> str:
    pkg = manifest.PACKAGES[package]
    L = ["# 补充说明:%s(%s)%s" % (pkg["title"], package, version), ""]
    L += ["本包由测试仓库的发布标签经转化脚本 `delivery/transform.py` 自动生成,**不要手工修改**(下一次生成会覆盖;"
          "转化脚本发现手工改动会停下)。要改内容,改测试仓库再重新生成。", ""]
    L += ["## 1. 版本对应", ""]
    L += _table(("项", "值"), [
        ("交付版本", version),
        ("来源", "测试仓库 %s,标签 `%s`,提交 `%s`(%s)" % (source["repo"], source["ref"], source["commit"][:10], source["date"][:10])),
        ("对应镜像", "`%s:%s`" % (pkg["image"], _image_tag(source["ref"], manifest))),
        ("上一次交付", "`%s`(来源 `%s`)" % (prev["version"], prev["ref"]) if prev else "无(首次交付)"),
    ])
    L += ["", "## 2. 包内容", ""]
    L += ["- `AGENTS.md`:根提示词(%s)。" % ("诊断环境" if package == "diag" else "知识库导入环境,由测试仓库的 AGENTS-kb-import.md 改名")]
    common = sorted(p for p in (root / "common").rglob("*") if p.is_file())
    L += ["- `common/`:%d 个文件(两个包共用的知识库库、连接层、报告存档等,加本包专用的模块)。" % len(common)]
    if pkg["whitelist"]:
        n = len(list((root / "scripts" / "registry").glob("*/*.yaml")))
        L += ["- `scripts/registry/`:技能运行时执行的 %d 条白名单脚本定义;`whitelist/`:给中间件团队导入的 DML。" % n]
    L += ["- `%s`:%s。" % (dst, desc) for _, dst, desc in pkg.get("extra", ())]
    L += ["- `skills/`:%d 个技能:" % len(pkg["skills"]), ""]
    L += _table(("技能", "版本"), [(s, skill_version(root / "skills" / s / "SKILL.md")) for s in pkg["skills"]])
    L += ["", "部署方式与镜像里一致:`common/`%s与 `skills/` 下的各个技能目录放进 opencode 的技能目录(镜像里是 "
          "`/opt/agent/config/opencode/skills/`,技能直接在这一层,不再套 `skills/`),`AGENTS.md` 放进 opencode 的配置目录"
          "(镜像里是 `/opt/agent/config/opencode/`)。" % ("、`scripts/`" if pkg["whitelist"] else ""), ""]
    # 2026-11-17 现场:只放了 common/ 与技能目录、漏了 scripts/,又没设 GSDB_SKILLS_DIR —— 部署自检读不到脚本定义中途退出,
    # 补充说明里的命令照抄也找不到文件。两条都写成必做项
    must = ["**`GSDB_SKILLS_DIR` 必须设置**:在启动 opencode 的环境里设成技能目录(例如 `export GSDB_SKILLS_DIR=/workspace/.opencode/skills`)。"
            "根提示词里的命令、下面的自检与耗时统计都按它找脚本;没设时照抄命令会提示找不到文件。"]
    if pkg["whitelist"]:
        must.insert(0, "**`scripts/` 必须和 `common/` 一起放**:放在技能目录下、与 `common/` 同一层(`$GSDB_SKILLS_DIR/scripts/registry/`)。"
                       "平时问答不读它(技能按脚本名调中间件),但部署自检要拿它和中间件登记的清单比对;漏了,"
                       "自检的「白名单」「白名单切库标注」两项会标「无法比对」。")
    L += ["- " + m for m in must] + [""]
    # 2026-11-06 行方升级:GSDB_HOME 不设时就是技能目录下的 common/,本地配置与会话都在里面,整包覆盖会把配置冲掉
    L += ["**升级时保留本地状态**:`GSDB_HOME` 不设时就是技能目录下的 `common/`,里面的 `config.yaml`(连接方式、中间件地址)、"
          "`credentials/`、`sessions/` 是本地状态。覆盖前先备份 `common/config.yaml`,覆盖后还原(包里的 `common/config.yaml` "
          "只是模板);`credentials/`、`sessions/` 包里没有,不受影响。换完新开对话:旧对话里读进来的还是旧版技能说明。", ""]
    if "gaussdb-login" in pkg["skills"]:
        # 2026-11-08 行方测试报告:一半的「失败」是环境问题(新包没装进运行目录、白名单没导全、受限账号、监控开关),
        # 用到时才以 HTTP 400 冒出来;装完先自检,一次查清、并写明找谁
        L += ["**装完先自检**:登录任一实例后运行 `python3 $GSDB_SKILLS_DIR/gaussdb-login/scripts/selfcheck.py --session <句柄>`"
              "(或在对话里说「做一次部署自检」)。它核对技能包版本、`GSDB_SKILLS_DIR`、知识库目录、白名单是否导全、白名单切库标注、"
              "是否切到了业务库、关键脚本能否执行(含执行账号权限)、数据库监控开关,逐项给出结论和「找谁」;某一项查不了只标这一项,其余照常查完。登录表格里的「技能包」一行"
              "显示正在运行的版本,与本包版本对不上说明新包没装进技能目录。", ""]
    if (root / "common" / "timing" / "cli.py").is_file():
        # 2026-11-16 现场反馈一次交互十几分钟:每一步的用时由脚本统计出来,不调模型、不改提示词
        L += ["**看每次交互慢在哪**(v1.4.14 起):在智能体所在的机器 / 容器里运行 "
              "`python3 $GSDB_SKILLS_DIR/common/timing/cli.py --last 3 --html /tmp/耗时报告.html`,"
              "(没设 `GSDB_SKILLS_DIR` 时写完整路径,例如 `python3 /workspace/.opencode/skills/common/timing/cli.py --last 3 --html /tmp/耗时报告.html`),"
              "按「提问 → 每一轮模型调用 → 等首字 / 思考与输出 / 工具(其中 GRMP 与数据库)/ 空档」统计用时并写出慢在哪;"
              "`--list` 列出最近的提问,`--since` / `--until` 按时间段取,`--text` 出文本版。纯脚本统计,不调模型、不联网、不改任何数据。"
              "默认是摘要版(只有时间、技能与脚本名、token 数);加 `--detail` 带思考原文、命令参数与工具输出(令牌、会话句柄、IP 自动遮掉),只在内部查看。"
              "GRMP 每次调用的用时由技能自动记在技能状态目录的 `logs/grmp-timing.log`(只有时间、脚本名与行数,不含参数值与结果)。", ""]
    L += _deps(manifest, package)
    L += _env(manifest, package)
    if pkg["whitelist"]:
        L += _whitelist(root, whitelist_diff)
    L += ["## %d. 根规范目录约定" % (6 if pkg["whitelist"] else 5), "", manifest.ROOTSPEC, ""]
    L += ["## %d. 变更摘要" % (7 if pkg["whitelist"] else 6), ""]
    if changes is None:
        L += ["首次交付,无上一版可比。", ""]
    elif not changes:
        L += ["与上一次交付相比,本包涉及的代码没有提交。", ""]
    else:
        L += ["测试仓库里 `%s..%s` 之间涉及本包的提交:" % (prev["ref"], source["ref"]), ""]
        L += ["- " + c for c in changes] + [""]
    return "\n".join(L).rstrip("\n") + "\n"


def _deps(manifest, package: str) -> List[str]:
    L = ["## 3. 依赖", "", "- Python:%s" % manifest.PYTHON, ""]
    L += _table(("Python 包", "版本", "用途"), [(k, v[0], v[1]) for k, v in manifest.PY_DEPS.items() if package in v[2]])
    L += [""]
    L += _table(("程序", "用途"), [(k, v[0]) for k, v in manifest.SYS_DEPS.items() if package in v[1]])
    return L + [""]


def _env(manifest, package: str) -> List[str]:
    rows = [(k, v[1], v[2]) for k, v in sorted(manifest.ENV.items()) if package in v[0]]
    return ["## 4. 环境变量", ""] + _table(("变量", "必填", "说明"), rows) + [""]


def _whitelist(root: pathlib.Path, diff: Optional[Dict[str, List[str]]]) -> List[str]:
    dml = (root / "whitelist" / "scripts.sql").read_text(encoding="utf-8")
    total = len(re.findall(r"^INSERT INTO", dml, re.M))
    deltas = sorted(p.name for p in (root / "whitelist").glob("scripts-delta-*.sql"))
    L = ["## 5. SQL 白名单", "",
         "全部是只读查询,只访问系统视图与系统表。全量 `whitelist/scripts.sql` 共 %d 条(第一次部署导这一份);" % total,
         "升级按顺序导增量:%s。逐条的用途、参数与 SQL 全文见 `whitelist/白名单脚本清单.md`;每份增量文件末尾有导入后自检。" % "、".join(deltas), ""]
    if diff is None:
        L += ["相对上一次交付:首次交付。", ""]
    else:
        parts = ["%s %d 条%s" % (k, len(v), (":" + "、".join("`%s`" % x for x in v)) if v else "")
                 for k, v in (("新增", diff["added"]), ("删除", diff["removed"]), ("正文变更", diff["changed"]))]
        L += ["相对上一次交付:" + ";".join(parts) + "。", ""]
    return L
