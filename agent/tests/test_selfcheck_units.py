"""部署自检(gaussdb-login/scripts/selfcheck.py,2026-11-08 加)。

行方测试报告里一半的「失败 / 受限」是环境问题:新包没装进运行目录、白名单没导全、中间件跑不了多条语句、
受限账号没权限、监控开关没开、知识库目录不存在。自检要一次把它们都点出来,并说清该找谁。
"""
import importlib.util
import pathlib
import sys
from dataclasses import replace

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.access import QueryError  # noqa: E402
from common.config import Connection  # noqa: E402
from common.grmp import routing  # noqa: E402
from common.grmp.registry import Registry  # noqa: E402

_SCRIPTS = _ROOT / "skills" / "gaussdb-login" / "scripts"
sys.modules.pop("render", None)
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("selfcheck_under_test", _SCRIPTS / "selfcheck.py")
sc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sc
_spec.loader.exec_module(sc)
sys.path.remove(str(_SCRIPTS))

CONN = Connection(name="10-0-0-9-appdb", type="gaussdb", host="127.0.0.1", port=8769, database="appdb",
                  user="grmp", driver="grmp", data_ip="10.0.0.9", app="api", route_db="appdb",
                  compat="A")          # v1.5.1:导完本版增量后登录探针取得到兼容模式(取不到时自检报「！」)
GOOD_TOP = {"unique_sql_id": "1", "query": "select 1", "calls": "1", "total_sec": "1", "avg_ms": "1", "rows": "1",
            "cpu_sec": "0", "io_sec": "0", "rows_written": "0", "user_name": "app"}


class _Client:
    """行方接口一的假清单。dbname:哪些脚本声明了 dbName(默认 = 按本版划分,行方已按增量导入)。"""

    def __init__(self, names, dbname=None):
        self._names = list(names)
        self._dbname = set(routing.BUSINESS_DB_SCRIPTS if dbname is None else dbname)

    def list_operations(self):
        return [{"cmd_name": n} for n in self._names]

    def names(self):
        return set(self._names)

    def registered(self, n):
        return n in self._names

    def declares(self, n, p):
        return p == routing.PARAM and n in self._dbname and n in self._names


class _Runner:
    def __init__(self, names, broken=(), old_top=False, storage_null=False, multi=True, switches=None,
                 dbname=None, probe_db="appdb"):
        self.client = _Client(names, dbname)
        self.probe_db = probe_db
        self.broken, self.old_top, self.storage_null, self.multi = set(broken), old_top, storage_null, multi
        self.switches = switches or {"enable_stmt_track": "on", "track_functions": "all",
                                     "enable_resource_track": "on", "enable_wdr_snapshot": "on"}

    def run(self, script, values=None):
        if script in self.broken:
            raise QueryError("中间件返回 HTTP 400:agent诊断脚本执行失败")
        if script == "explain.multi_stmt_probe":
            if not self.multi:
                raise QueryError("中间件返回 HTTP 400:agent诊断脚本执行失败")
            return [{"ok": "1"}]
        if script == "health.storage":
            return [{"kind": "database", "name": "appdb", "bytes": "100"},
                    {"kind": "database", "name": "other", "bytes": "" if self.storage_null else "5"}]
        if script == "topsql.top_sql":
            row = dict(GOOD_TOP)
            if self.old_top:
                for k in ("cpu_sec", "io_sec", "rows_written", "user_name"):
                    row.pop(k)
            return [row]
        if script == "health.slow_sql":
            return [{"unique_sql_id": "1", "user_name": "app"}]
        if script == "config.settings":
            return [{"name": k, "setting": v} for k, v in self.switches.items()]
        if script == routing.PROBE:
            return [{"db": self.probe_db, "usr": "grmp"}]
        return [{"x": "1"}]


def _run(monkeypatch, tmp_path, runner, capsys, kb=True):
    kb_dir = tmp_path / "kb"
    if kb:
        kb_dir.mkdir()
    monkeypatch.setenv("GSDB_KB_DIR", str(kb_dir))
    monkeypatch.setenv("GSDB_SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(sc.config, "resolve", lambda name=None: CONN)
    monkeypatch.setattr(sc.access, "runner_for", lambda conn: runner)
    rc = sc.main([])
    return rc, capsys.readouterr().out


def test_a_healthy_environment_passes(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry().names()), capsys)
    assert rc == 0 and "全部正常" in out and sc.package_version() in out


def test_a_complete_whitelist_still_says_bodies_are_not_checked(monkeypatch, tmp_path, capsys):
    """2026-11-19 补测:清单接口只回脚本名与参数、没有正文。只改正文的增量(如 || 改 concat)没导时这一项照样 ✓,
    现场会以为修复已经生效 —— 在 ✓ 的说明后面点明这一点和最新一份增量的文件名(取自 deltas,不写死)。级别保持 ✓。"""
    from common.grmp import deltas
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry().names()), capsys)
    line = next(ln for ln in out.splitlines() if ln.startswith("| 白名单 |"))
    assert "| ✓ |" in line and rc == 0 and "全部正常" in out, "级别不变,导全了不会一直显示 !"
    assert "脚本正文是否为最新本项看不出(中间件清单接口不返回正文)" in line
    assert "请确认已导到 %s" % deltas.DELTAS[-1].file in line


def test_the_bank_environment_is_explained_item_by_item(monkeypatch, tmp_path, capsys):
    names = [n for n in Registry().names() if not n.startswith("health.xlog")]      # 白名单没导全
    runner = _Runner(names, broken={"health.slow_sql"}, old_top=True, storage_null=True, multi=False,
                     switches={"enable_stmt_track": "on", "track_functions": "none"})
    rc, out = _run(monkeypatch, tmp_path, runner, capsys, kb=False)
    assert rc == 1
    assert "缺 " in out and "health.xlog_position" in out and "中间件团队" in out
    assert "从 scripts-delta-20261105.sql 起按顺序导完 7 份增量" in out and "scripts-delta-20261123.sql" in out, \
        "按缺的脚本推出现场导到了哪里,列出从那一份起的全部增量(2026-11-19:原先只叫人导本版那一份)"
    assert "按补充说明「SQL 白名单」一节导入增量" not in out
    assert "多条语句" not in out, "v1.4.13 起不再交付两语句脚本,自检不再探测"
    assert "health.slow_sql 执行失败" in out and "中间件日志" in out
    assert "topsql.top_sql 是旧版本" in out and "user_name" in out
    assert "1 个库 / 表空间没有权限查看" in out and "运维 DBA" in out
    assert "track_functions" in out and "存储过程排行" in out
    assert "知识库目录" in out and "不存在" in out


def test_without_a_session_it_says_to_log_in_first(monkeypatch, tmp_path, capsys):
    from common.config import ConfigError

    def _no(name=None):
        raise ConfigError("本对话未登录")
    monkeypatch.setenv("GSDB_SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(sc.config, "resolve", _no)
    assert sc.main([]) == 1
    assert "先用 gaussdb-login 登录再自检" in capsys.readouterr().out


def test_gsql_mode_skips_the_middleware_whitelist(monkeypatch, tmp_path, capsys):
    direct = replace(CONN, driver="gsql")
    monkeypatch.setenv("GSDB_KB_DIR", str(tmp_path))
    monkeypatch.setenv("GSDB_SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(sc.config, "resolve", lambda name=None: direct)
    monkeypatch.setattr(sc.access, "runner_for", lambda conn: _Runner([]))
    assert sc.main([]) == 0
    assert "| 白名单 |" not in capsys.readouterr().out


def test_an_empty_list_is_explained_as_an_instance_attribute_mismatch(monkeypatch, tmp_path, capsys):
    """2026-11-08 行方批量登录 92 个库,2 个拿到空清单:白名单不配在目标库上,是中间件按实例属性匹配脚本时没匹配上。"""
    rc, out = _run(monkeypatch, tmp_path, _Runner([]), capsys)
    assert rc == 1 and "脚本清单为空" in out and "集中式" in out and "灌" not in out


# ---- 现场形态:技能目录下只有 common 与各技能,没有 scripts/(2026-11-17 现场照片)

def test_a_missing_script_registry_does_not_stop_the_report(monkeypatch, tmp_path, capsys):
    """现场安装时只放了 common/ 与技能目录、漏了 scripts/:读不到本地脚本定义,原来整份报告在「白名单核对」抛
    RegistryError 退出,后面的脚本试跑、监控开关都没跑。现在这两项标「无法比对」并说清下一步,其余照常跑完。"""
    monkeypatch.setenv("GRMP_REGISTRY", str(tmp_path / "skills" / "scripts" / "registry"))
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry(_ROOT / "scripts" / "registry").names()), capsys)
    assert "| 白名单 | ！ | 无法比对" in out and "| 白名单切库标注 | ！ | 无法比对" in out
    assert "scripts/" in out and "common/ 同一层" in out and "部署方" in out
    assert "已切到「appdb」" in out, "业务库切换不依赖本地脚本定义,照常检查"
    assert "health.overview 正常" in out and "参数 enable_stmt_track" in out, "后面各项照常跑完"
    assert "Traceback" not in out and rc == 0, "只是比对不了,不算功能不可用"


def test_an_unexpected_error_in_one_check_only_marks_that_check(monkeypatch, tmp_path, capsys):
    def boom(runner):
        raise RuntimeError("意外")
    monkeypatch.setattr(sc, "check_scripts", boom)
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry().names()), capsys)
    assert "| 关键脚本试跑 | ✕ | 自检这一项出错:意外" in out and rc == 1
    assert "参数 enable_stmt_track" in out and "| 白名单 | ✓" in out, "其余各项照常输出"


# ---- 切到业务库(dbName,2026-11-11)

def test_routing_is_reported_when_the_session_switched(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry().names()), capsys)
    assert "已切到「appdb」" in out and "与本版一致" in out


def test_a_switch_that_did_not_happen_is_a_failure(monkeypatch, tmp_path, capsys):
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry().names(), probe_db="postgres"), capsys)
    assert rc == 1 and "中间件没按 dbName 切库" in out


def test_an_old_whitelist_explains_how_to_get_the_switch(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys.modules[__name__], "CONN", replace(CONN, route_db=""))     # 旧白名单:登录时切不过去
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry().names(), dbname=()), capsys)
    assert "行方白名单还没有带 dbName 的脚本" in out and "--database <业务库>" in out


def test_marks_that_differ_from_this_version_are_listed(monkeypatch, tmp_path, capsys):
    dbname = (set(routing.BUSINESS_DB_SCRIPTS) - {"sqltune.indexes"}) | {"topsql.snap_statement"}
    rc, out = _run(monkeypatch, tmp_path, _Runner(Registry().names(), dbname=dbname), capsys)
    assert "1 条该切库的没带 dbName(sqltune.indexes)" in out
    assert "1 条读 SQL 历史 / WDR 快照 / 实例级视图的带了 dbName(topsql.snap_statement)" in out
    assert "从 scripts-delta-20261111.sql 起按顺序导完 5 份增量" in out and "按本版增量白名单导入后一致" not in out


def test_a_gap_no_delta_explains_asks_for_the_whitelist_export(monkeypatch, tmp_path, capsys):
    names = [n for n in Registry().names() if n != "health.overview"]          # 全量里就有,不是哪份增量新增的
    rc, out = _run(monkeypatch, tmp_path, _Runner(names), capsys)
    assert "health.overview" in out and "把中间件里的白名单导出交给我方核对" in out


def test_old_scripts_this_version_no_longer_calls_are_not_listed(monkeypatch, tmp_path, capsys):
    """2026-11-12 回归:v1.4.13 不再调用的 7 条 *_schema 两语句脚本,行方可以保留(增量不生成删除),
    它们在 20261111 里登记成带 dbName —— 自检不能因此一直报「切库标注不一致、找中间件团队」,导完增量也消不掉。"""
    old = "explain.plan_text_schema"
    assert old not in Registry().names()
    dbname = set(routing.BUSINESS_DB_SCRIPTS) | {old}
    rc, out = _run(monkeypatch, tmp_path, _Runner(list(Registry().names()) + [old], dbname=dbname), capsys)
    assert old not in out and "与本版一致" in out and "| 白名单切库标注 | ！" not in out


class _Names:
    def __init__(self, names):
        self._names = list(names)

    def names(self):
        return list(self._names)


def test_used_names_skips_common_once_it_sits_in_the_skills_dir(tmp_path):
    """部署后 common/ 在技能目录里;common/grmp/routing.py 的切库名单是数据,不是调用(11-11 测试环境查出误报)。"""
    (tmp_path / "common" / "grmp").mkdir(parents=True)
    (tmp_path / "common" / "grmp" / "routing.py").write_text("NAMES = {\"perf.locks\", \"sqltune.indexes\"}\n", encoding="utf-8")
    (tmp_path / "gaussdb-x" / "scripts").mkdir(parents=True)
    (tmp_path / "gaussdb-x" / "scripts" / "x.py").write_text("runner.run(\"sqltune.indexes\")\n", encoding="utf-8")
    used = sc.used_names(_Names(["perf.locks", "sqltune.indexes"]), tmp_path)
    assert used == {"sqltune.indexes"}


def test_used_names_is_the_same_in_the_deployed_layout_and_equals_the_delivery(tmp_path):
    """按部署布局(技能目录下放 common/)算出的「用到的」与仓库布局一致,也正好是交付白名单的那些条。"""
    import re
    import shutil
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for d in (_ROOT / "skills").iterdir():
        if d.is_dir():
            shutil.copytree(d, tmp_path / d.name, ignore=ignore)
    shutil.copytree(_ROOT / "common", tmp_path / "common", ignore=ignore)
    reg = Registry()
    sql = (_ROOT.parent / "docs" / "delivery" / "scripts.sql").read_text(encoding="utf-8")
    delivered = {n for n in reg.names() if re.search(r"%s" % re.escape(n), sql)}
    assert sc.used_names(reg, tmp_path) == sc.used_names(reg) == delivered
