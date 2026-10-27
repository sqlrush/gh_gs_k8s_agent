"""entrypoint.sh 的契约——只 grep 结构,不跑它。

报告只读端口是大盘的数据口:entrypoint 要导出 GSDB_REPORTS_DIR、在 opencode 之后拉起
serve-reports、停机时把它和另外两个后台循环一起收掉。漏任何一条都是「大盘无数据且不报错」。
"""
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_EP = (_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")


def test_reports_dir_is_exported_under_nas_me():
    assert re.search(r'^export GSDB_REPORTS_DIR=\$NAS_ME/reports\b', _EP, re.M), "报告目录必须在 NAS 上,随 Pod 重建不丢"


def test_reports_server_is_started_after_opencode_and_backgrounded():
    oc = _EP.index("opencode serve --hostname")
    m = re.search(r'\$PODCTL serve-reports --dir "\$GSDB_REPORTS_DIR" --port 4097 --kb-dir "\$GSDB_KB_DIR" &\s*\nREPORTS=\$!', _EP)
    assert m and m.start() > oc, "serve-reports 要在 opencode serve 之后、放后台并记 PID,且带 --kb-dir(大盘知识库页签的数据)"


def test_reports_server_is_killed_with_the_other_background_loops():
    assert re.search(r'^kill "\$REFRESHER" "\$SYNCER" "\$REPORTS"', _EP, re.M), "停机要连报告服务一起收掉"


def test_the_platform_managed_pool_branch_is_gone():
    """v1.2.0 池化(POOL_MODE=1,平台管理的预热沙箱)由固定池化取代,入口里不再有它。"""
    assert 'POOL_MODE:-0}" = "1"' not in _EP and "pool-run" not in _EP


def test_steward_mode_execs_the_steward_before_any_nas_step():
    m = re.search(r'^if \[ "\$\{POOL_MODE:-0\}" = "steward" \]; then\n(.*?)^fi$', _EP, re.M | re.S)
    assert m and "exec python3 -u /opt/agent/podctl.py pool-steward" in m.group(1)
    first_nas = min(_EP.index(k) for k in ('"$NAS_ME', "$NAS_ME/"))
    assert m.end() < first_nas, "管家分支要在按需模式的第一个 NAS 步骤之前"


def test_slot_mode_execs_the_slot_daemon_before_touching_anything():
    """固定池化的槽位:上一个使用者的文件还在 /data、/tmp 里,清盘之前不能碰它们(也不能 source 任何东西)。"""
    m = re.search(r'^if \[ "\$\{POOL_MODE:-0\}" = "slot" \]; then\n(.*?)^fi$', _EP, re.M | re.S)
    assert m, "没有 POOL_MODE=slot 分支"
    lines = [l.strip() for l in m.group(1).splitlines() if l.strip() and not l.strip().startswith("#")]
    assert lines == ["exec python3 -E -s -u /opt/agent/podctl.py pool-slot"], "分支里只能有这一行 exec"
    before = [l.strip() for l in _EP[:m.start()].splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert before == ["set -euo pipefail"], "槽位分支要是第一条命令:之前连变量都不设"
    assert m.end() < _EP.index('if [ "${POOL_MODE:-0}" = "steward" ]'), "要在管家分支之前"


def test_clip_receiver_runs_only_in_the_import_container_and_is_stopped_with_the_rest():
    """网页剪藏接收(2026-10-27):模板给导入容器 KB_CLIP_RECEIVER=1,诊断容器没有这个端口;不是就绪条件,放后台。"""
    m = re.search(r'^if \[ "\$\{KB_CLIP_RECEIVER:-0\}" = "1" \]; then\n(.*?)^fi$', _EP, re.M | re.S)
    assert m, "serve-clip 要由 KB_CLIP_RECEIVER 开关"
    body = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    assert body == ["$PODCTL serve-clip --port 4095 &", "CLIPD=$!"]
    assert m.start() > _EP.index("opencode serve --hostname"), "要在 opencode 之后(它要往 opencode 里发起导入)"
    assert re.search(r'^CLIPD=""$', _EP[:m.start()], re.M), "set -u 下没开剪藏时 $CLIPD 也要有定义"
    assert re.search(r'^kill "\$REFRESHER" "\$SYNCER" "\$REPORTS" \$CLIPD ', _EP, re.M), "停机要连剪藏接收一起收掉"
