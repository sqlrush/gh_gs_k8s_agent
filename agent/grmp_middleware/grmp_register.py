"""脚本注册工具 —— 对应客户侧的「版本 DML 发布」环节。

**这是发布期工具，不是运行期接口。** 客户的 GRMP 没有脚本管理 API
（「由于安全原因，目前脚本仅能通过版本 dml 带出」），白名单的写权限归
发布流程，读与执行权限归 agent。本工具因此刻意做成命令行，不挂 HTTP ——
挂上去就等于本地放开了客户不给的权限，explain 之类的脚本在本地能动态
注册、到客户环境注册不了，又是一个本地测不出来的落差。

用法：
    python3 -m grmp_middleware.grmp_register --registry scripts/registry \\
        --db ~/.gdaa/grmp/script_config.db --dml-out docs/delivery/scripts.sql
"""
from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import sys
from typing import List, Sequence, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common.grmp import script as sc  # noqa: E402
from grmp_middleware.grmp_mock import dml, risk, store as st  # noqa: E402
from common.grmp.placeholder import ParamError  # noqa: E402

DEFAULT_REGISTRY = "scripts/registry"
DEFAULT_DB = "~/.gdaa/grmp/script_config.db"


def discover(registry: pathlib.Path) -> List[pathlib.Path]:
    """按路径排序，使注册顺序稳定 —— id 分配才可复现。"""
    return sorted(registry.rglob("*.yaml"))


def _timestamp() -> str:
    """客户样例的 create_time 精确到毫秒：2026-07-03 19:36:58.978。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def load_all(paths: Sequence[pathlib.Path]) -> Tuple[List[sc.ScriptRecord], List[str]]:
    """加载并硬校验。返回 (记录, 错误列表)；有错时不入库任何一条。"""
    records: List[sc.ScriptRecord] = []
    errors: List[str] = []
    seen_names = {}
    for path in paths:
        try:
            rec = sc.load_script(path)
        except (sc.ScriptError, ParamError) as exc:
            errors.append(str(exc))
            continue
        if rec.script_name in seen_names:
            errors.append(
                "%s: 逻辑名 %s 与 %s 重复"
                % (path, rec.script_name, seen_names[rec.script_name])
            )
            continue
        seen_names[rec.script_name] = path
        records.append(rec)
    return records, errors


SKILLS_SUBDIR = "skills"


def scripts_no_skill_calls(registry: pathlib.Path) -> List[str]:
    """列出注册目录里**没有任何 skill 调用**的脚本逻辑名。

    判据是逻辑名全串在 skills/ 的 .py 源码里出现过。skill 里两种写法都有：

        runner.run("health.overview")            # 字面量
        TOP_SQL_SCRIPT = "topsql.top_sql"        # 常量,再 run(TOP_SQL_SCRIPT)

    两种都是全串,所以搜全串就够，不必解析。**刻意不加「短名也算」的兜底** ——
    试过，它把 perf.memory / perf.locks 全放过了（"memory"/"locks" 在别处
    当维度名用着）。兜底比漏报危险：它让检查看起来跑过了。
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    skills = root / SKILLS_SUBDIR
    if not skills.is_dir():          # 单测里指到临时目录，没有 skills/
        return []
    text = "\n".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in skills.rglob("*.py")
    )
    names = ["%s.%s" % (f.parent.name, f.stem)
             for f in sorted(pathlib.Path(registry).glob("*/*.yaml"))]
    return [n for n in names if n not in text]


def report_risks(records: Sequence[sc.ScriptRecord]) -> List[str]:
    """风险标注 —— 只报告，不拦截。客户环境能跑通的脚本，本地不能拒绝。"""
    lines: List[str] = []
    for rec in records:
        for item in risk.assess(rec.script_content, rec.params):
            lines.append("  [%s] %s：%s" % (item.code, rec.script_name, item.detail))
    return lines


def main(argv: Sequence[str] = None) -> int:
    parser = argparse.ArgumentParser(description="注册 GRMP 诊断脚本")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="重名时覆盖（保持原 id 不变）。不给此参数时重名一律拒绝。",
    )
    parser.add_argument("--dml-out", default=None, help="导出客户格式 INSERT DML 的路径")
    parser.add_argument(
        "--include-unused",
        action="store_true",
        help="导出时连「没有任何 skill 调用」的脚本一起带上。默认拒绝导出。",
    )
    parser.add_argument(
        "--exclude-unused",
        action="store_true",
        help="导出时剔掉「没有任何 skill 调用」的脚本。交付通常用这个。",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("GRMP_REGISTER_USER", "grmp-register"),
        help="写入 create_user/last_modify_user。交付给客户的 DML 应填真实工号。",
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验与出报告，不写库")
    args = parser.parse_args(argv)

    registry = pathlib.Path(args.registry).expanduser()
    if not registry.is_dir():
        print("脚本目录不存在：%s" % registry, file=sys.stderr)
        return 2

    paths = discover(registry)
    if not paths:
        print("脚本目录为空：%s" % registry, file=sys.stderr)
        return 2

    records, errors = load_all(paths)
    if errors:
        print("校验失败，未注册任何脚本：", file=sys.stderr)
        for line in errors:
            print("  - %s" % line, file=sys.stderr)
        return 1

    print("校验通过：%d 条脚本" % len(records))
    risks = report_risks(records)
    if risks:
        print("\n风险标注（放行，但需人工确认 —— 这些脚本在客户环境同样存在该风险）：")
        for line in risks:
            print(line)
    else:
        print("风险标注：无")

    if args.dml_out and not (args.include_unused or args.exclude_unused):
        unused = scripts_no_skill_calls(registry)
        if unused:
            print(
                "\n拒绝导出交付 DML：以下 %d 条脚本没有任何 skill 调用。" % len(unused),
                file=sys.stderr)
            for n in unused:
                print("  - %s" % n, file=sys.stderr)
            print(
                "\n导出的每一条 INSERT 都要灌进客户生产库的 script_config，"
                "客户得为它走变更评审。\n"
                "这些多半是中间件自测用的脚本（perf.* 是动态性能视图那组验证场景，"
                "session.* 给 grmp_demo.sh 用）—— 该留在仓库里，不该混进交付物。\n"
                "确实要一起交付就加 --include-unused；否则用 --registry 指一个"
                "只含交付脚本的目录。",
                file=sys.stderr)
            return 1

    if args.dry_run:
        if args.dml_out:
            # 原先这里直接 return 0，**而 --dml-out 的导出代码在下面** ——
            # 于是 `--dry-run --dml-out X` 静默什么都不做还返回 0，调用方以为
            # 生成好了。实测被它骗过一次：交付 DML 明明没更新，命令却成功了。
            print(
                "\n--dry-run 不能与 --dml-out 同用：DML 里的 id 来自脚本库"
                "（每条 INSERT 都要带 id），不写库就分配不出 id。\n"
                "要生成交付 DML，去掉 --dry-run：\n"
                "  python3 -m grmp_middleware.grmp_register --registry %s \\\n"
                "      --db ~/.gdaa/grmp/script_config.db --replace \\\n"
                "      --exclude-unused --dml-out %s"
                % (args.registry, args.dml_out), file=sys.stderr)
            return 2
        print("\n--dry-run：未写库")
        return 0

    when = _timestamp()
    store = st.ScriptStore(pathlib.Path(args.db).expanduser())
    stored: List[sc.ScriptRecord] = []
    try:
        for rec in records:
            stored.append(
                store.register(rec.stamped(args.user, when), replace=args.replace)
            )
    except st.StoreError as exc:
        print("\n注册中止：%s" % exc, file=sys.stderr)
        return 1

    print("\n已注册：")
    for rec in stored:
        print("  id=%-4s %s" % (rec.id, rec.script_name))

    if args.dml_out:
        out = pathlib.Path(args.dml_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        shipped = stored
        if args.exclude_unused:
            drop = set(scripts_no_skill_calls(registry))
            shipped = [r for r in stored if r.script_name not in drop]
            # 剔掉了什么必须逐条说出来 —— 静默少几条，客户那边就表现成
            # 「某个 skill 突然报脚本不存在」，而发布记录上看不出少了谁
            print("\n已剔除 %d 条无人调用的脚本（未进交付 DML）：" % len(drop))
            for n in sorted(drop):
                print("  - %s" % n)
        out.write_text(dml.script_file(shipped), encoding="utf-8")
        print("\n交付 DML 已写出：%s" % out)
        print("  注意：create_user 当前为 %r，交付前应改成真实工号。" % args.user)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
