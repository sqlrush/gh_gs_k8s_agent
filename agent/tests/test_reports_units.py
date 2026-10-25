"""common.reports —— 报告存档:目录未设即不存档;原子写;latest/index;保留 N 份;失败只 warn。"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common import reports  # noqa: E402


def test_unset_env_means_no_archive(monkeypatch, tmp_path):
    monkeypatch.delenv("GSDB_REPORTS_DIR", raising=False)
    assert reports.reports_dir() is None
    assert reports.archive("health", {"overall": 0}) is None
    assert not list(tmp_path.iterdir())


def test_archive_writes_file_latest_and_index(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    p = reports.archive("health", {"overall": 2, "findings": [{"severity": 2}, {"severity": 1}]},
                        now="20260922T060312Z")
    assert p == tmp_path / "health" / "20260922T060312Z.json"
    assert json.loads(p.read_text(encoding="utf-8"))["overall"] == 2
    latest = json.loads((tmp_path / "health" / "latest.json").read_text(encoding="utf-8"))
    assert latest["overall"] == 2
    idx = json.loads((tmp_path / "health" / "index.json").read_text(encoding="utf-8"))
    assert idx == [{"at": "20260922T060312Z", "file": "20260922T060312Z.json",
                    "overall": 2, "counts": {"1": 1, "2": 1}}]


def test_explicit_name_is_used_and_index_dedupes_by_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    reports.archive("sqltune", {"a": 1}, name="1a9f", now="20260922T000000Z")
    reports.archive("sqltune", {"a": 2}, name="1a9f", now="20260922T000100Z")
    idx = json.loads((tmp_path / "sqltune" / "index.json").read_text(encoding="utf-8"))
    assert [e["file"] for e in idx] == ["1a9f.json"], "同名覆盖时 index 不该出现两条"
    assert json.loads((tmp_path / "sqltune" / "1a9f.json").read_text())["a"] == 2


def test_keep_prunes_oldest_only_within_skill_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    (tmp_path / "health").mkdir()
    (tmp_path / "health" / "stranger.json").write_text("{}", encoding="utf-8")   # 不在 index 里,不能被删
    for i in range(4):
        reports.archive("health", {"overall": 0}, keep=3, now="2026092%dT000000Z" % i)
    names = sorted(p.name for p in (tmp_path / "health").glob("*.json"))
    assert "20260920T000000Z.json" not in names, "最旧的一份该被删"
    assert "stranger.json" in names, "只删 index 里登记过的"
    idx = json.loads((tmp_path / "health" / "index.json").read_text(encoding="utf-8"))
    assert len(idx) == 3


def test_archive_failure_warns_and_never_raises(monkeypatch, tmp_path, capsys):
    """**存档失败不能拖垮技能。** 目录是个文件 → mkdir 失败 → 只 warn。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(blocker))
    assert reports.archive("health", {"overall": 0}) is None
    assert "[warn] 报告未存档" in capsys.readouterr().err


def test_append_jsonl(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    assert reports.append_jsonl("kb", "queries", {"q": "慢 SQL"}) is True
    assert reports.append_jsonl("kb", "queries", {"q": "锁"}) is True
    lines = (tmp_path / "kb" / "queries.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["q"] for x in lines] == ["慢 SQL", "锁"]


def test_write_is_atomic_no_tmp_left_behind(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    reports.archive("wdr", {"overall": 0}, now="20260922T000000Z")
    assert not list((tmp_path / "wdr").glob("*.tmp"))


# ---- 按实例分目录 ----------------------------------------------------------------

def test_instance_key_sanitizes_conn_name():
    assert reports.instance_key("api/10-0-0-9-postgres") == "api_10-0-0-9-postgres"
    assert reports.instance_key("og5") == "og5"
    assert reports.instance_key("") == "_default"
    assert reports.instance_key("../x") == ".._x", "斜杠必须换掉,键要能当路径段"


def test_archive_with_instance_goes_into_subdir_and_updates_targets(monkeypatch, tmp_path):
    """三个大盘都是按库统计的:一个人上午看 A 库下午看 B 库,latest/index 不能混。"""
    from common import config

    def _no_conn(name=None):
        raise config.ConfigError("未登录")
    monkeypatch.setattr(config, "resolve", _no_conn)     # 不让开发机上的 ~/.gdaa 影响结果
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    reports.archive("health", {"overall": 1, "conn": "api/10-0-0-9-postgres"},
                    instance="api/10-0-0-9-postgres", now="20260923T010000Z")
    reports.archive("health", {"overall": 2, "conn": "og5"}, instance="og5", now="20260923T020000Z")
    reports.archive("health", {"overall": 0, "conn": "og5"}, instance="og5", now="20260923T030000Z")
    assert (tmp_path / "health" / "api_10-0-0-9-postgres" / "latest.json").is_file()
    assert (tmp_path / "health" / "og5" / "latest.json").is_file()
    assert not (tmp_path / "health" / "latest.json").exists(), "分实例后技能根目录不该再有 latest"
    t = json.loads((tmp_path / "health" / "targets.json").read_text(encoding="utf-8"))
    assert [x["key"] for x in t] == ["og5", "api_10-0-0-9-postgres"], "按最近一次降序"
    assert t[0] == {"key": "og5", "conn": "og5", "last_at": "20260923T030000Z", "count": 2,
                    "label": "og5", "login": ""}, "解析不到连接时 label 退回 conn、login 留空"
    og5_idx = json.loads((tmp_path / "health" / "og5" / "index.json").read_text(encoding="utf-8"))
    assert [e["overall"] for e in og5_idx] == [2, 0], "各实例各自的 index,不混"


def _conn(**kw):
    from common.config import Connection
    base = dict(name="10-0-0-9-postgres", type="gaussdb", host="127.0.0.1", port=8779,
                database="postgres", user="u", driver="grmp", data_ip="10.0.0.9", app="api")
    base.update(kw)
    return Connection(**base)


def test_targets_carry_a_readable_label_and_a_login_phrase(monkeypatch, tmp_path):
    """大盘按实例分开,深挖开的新会话还没登录 —— 首句不说是哪个库,模型只能反过来问用户。

    targets.json 记下可读的实例名(侧栏显示)和登录说法(深挖首句用)。红线第 5 条允许出现
    用户自己连接的实例 IP 与库名。2026-09-23 v1.1.0 全面测试时发现。
    """
    from common import config
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "resolve", lambda name=None: _conn())
    reports.archive("health", {"overall": 1}, instance="api/10-0-0-9-postgres", now="20260923T010000Z")
    t = json.loads((tmp_path / "health" / "targets.json").read_text(encoding="utf-8"))[0]
    assert t["label"] == "10.0.0.9 / postgres"
    assert t["login"] == "登录 10.0.0.9 的 postgres 库"


def test_direct_mode_target_logs_in_by_connection_name(monkeypatch, tmp_path):
    from common import config
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "resolve",
                        lambda name=None: _conn(name="og", app="", driver="psycopg2", data_ip="", host="db1", port=5432))
    reports.archive("health", {"overall": 1}, instance="og", now="20260923T010000Z")
    t = json.loads((tmp_path / "health" / "targets.json").read_text(encoding="utf-8"))[0]
    assert t["label"] == "og / postgres"
    assert t["login"] == "用连接 og 登录"


def test_label_is_not_taken_from_a_different_connection(monkeypatch, tmp_path):
    """解析出来的连接和要存档的 conn 对不上时不用它 —— 宁可退回 conn,不能把 A 库的名字挂到 B 库上。"""
    from common import config
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "resolve", lambda name=None: _conn(name="10-0-0-12-postgres", data_ip="10.0.0.12"))
    reports.archive("health", {"overall": 1}, instance="api/10-0-0-9-postgres", now="20260923T010000Z")
    t = json.loads((tmp_path / "health" / "targets.json").read_text(encoding="utf-8"))[0]
    assert t["label"] == "api/10-0-0-9-postgres" and t["login"] == ""


def test_archive_hides_middleware_address_and_endpoint_path(monkeypatch, tmp_path):
    """红线第 5 条:内部接口路径、http:// 地址是机密,只有用户连接的实例 IP 与库名可以出现。

    模型的输出有自检,大盘没有 —— 采集失败时维度说明里带着
    「请求 /x/y/…/invoke 失败…(中间件 http://host:8781)」,原样画在每个用户的大盘上。
    2026-09-23 集群全链路测试截图发现。存档时就隐去,大盘、下载的 JSON 都干净。
    """
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    msg = ("不可用：请求 /corp/paas/aiops/grmp/diagnostic/agent/common-operations/invoke 失败："
           "Remote end closed connection without response（中间件 http://host.docker.internal:8781）")
    payload = {"dims": [{"dimension": "Overview", "available": False, "headline": msg, "note": msg,
                         "rows": [["/data/db/base/16384", "x"]]}],
               "sub_skills": [{"skill": "gaussdb-lockwait", "ok": False, "error": "连不上 https://10.1.2.3:443/api/v1/x"}],
               "rows": [{"query": "select a/b/c from t where u = 'http://x'"}]}
    p = reports.archive("health", payload, now="20260923T010000Z")
    d = json.loads(p.read_text(encoding="utf-8"))
    dim = d["dims"][0]
    for text in (dim["headline"], dim["note"], d["sub_skills"][0]["error"]):
        assert "http" not in text and "/invoke" not in text and "aiops" not in text, text
    assert "Remote end closed connection" in dim["headline"], "原因本身要留着"
    assert dim["rows"] == [["/data/db/base/16384", "x"]], "表格数据不动"
    assert d["rows"][0]["query"] == "select a/b/c from t where u = 'http://x'", "SQL 文本不动"


def test_index_records_how_many_dims_were_unavailable(monkeypatch, tmp_path):
    """全部采集失败时 overall=0,只看 overall 的历史条会画成绿色「正常」。"""
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    reports.archive("health", {"overall": 0, "dims": [{"available": False}, {"available": False}, {"available": True}]},
                    now="20260923T010000Z")
    reports.archive("kb", {"x": 1}, name="health")
    idx = json.loads((tmp_path / "health" / "index.json").read_text(encoding="utf-8"))
    assert idx[-1]["dims"] == {"total": 3, "na": 2}
    kb_idx = json.loads((tmp_path / "kb" / "index.json").read_text(encoding="utf-8"))
    assert "dims" not in kb_idx[-1], "没有维度的报告不记"


def test_archive_without_instance_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    reports.archive("kb", {"a": 1}, name="health")
    assert (tmp_path / "kb" / "health.json").is_file() and not (tmp_path / "kb" / "targets.json").exists()


# ---- 索引里登记着、文件却已不在 ---------------------------------------------------------
# 2026-10-24 测试环境:NAS 放在 Mac 的 /tmp 下,macOS 每天 0 点的 tmp_cleaner 删掉三天没碰过的文件。
# 报告写一次就不再动 → 被删;index.json 每次存档都重写 → 留下。大盘按索引取最新一份,整页 404。

def _three_reports(monkeypatch, tmp_path):
    from common import config

    def _no_conn(name=None):
        raise config.ConfigError("未登录")
    monkeypatch.setattr(config, "resolve", _no_conn)
    monkeypatch.setenv("GSDB_REPORTS_DIR", str(tmp_path))
    for i in range(3):
        reports.archive("topsql", {"rows": []}, name="2026101%dT000000Z.time" % i, instance="og5", now="2026101%dT000000Z" % i)
    return tmp_path / "topsql" / "og5"


def test_index_outlives_files_removed_behind_its_back(monkeypatch, tmp_path):
    """复现那个现象本身:文件被外部删掉之后,下一次存档之前索引仍登记着它们 —— 大盘照着索引去取就是 404。"""
    d = _three_reports(monkeypatch, tmp_path)
    for i in range(2):                                   # tmp_cleaner 的样子:最老的那几份被删,index.json 不动
        (d / ("2026101%dT000000Z.time.json" % i)).unlink()
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    assert len(idx) == 3 and not (d / idx[0]["file"]).exists()


def test_next_archive_drops_index_entries_whose_file_is_gone(monkeypatch, tmp_path, capsys):
    d = _three_reports(monkeypatch, tmp_path)
    for i in range(2):
        (d / ("2026101%dT000000Z.time.json" % i)).unlink()
    capsys.readouterr()
    reports.archive("topsql", {"rows": []}, name="20261013T000000Z.time", instance="og5", now="20261013T000000Z")
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    assert [e["file"] for e in idx] == ["20261012T000000Z.time.json", "20261013T000000Z.time.json"]
    assert all((d / e["file"]).is_file() for e in idx), "索引里只留文件还在的"
    t = json.loads((tmp_path / "topsql" / "targets.json").read_text(encoding="utf-8"))
    assert t[0]["count"] == 2, "侧栏的份数跟着改"
    assert "索引里有 2 份报告的文件已不在" in capsys.readouterr().err, "不能悄悄清:标准错误说一句"


def test_unreadable_file_is_not_treated_as_gone(monkeypatch, tmp_path, capsys):
    """权限、NFS 抖动这类读不准的,当它还在 —— 宁可索引里多一条,也不能误删登记。"""
    d = _three_reports(monkeypatch, tmp_path)
    real = reports.os.stat
    flaky = str(d / "20261010T000000Z.time.json")

    def _stat(p, *a, **kw):
        if str(p) == flaky:
            raise PermissionError(13, "Permission denied", flaky)
        return real(p, *a, **kw)
    monkeypatch.setattr(reports.os, "stat", _stat)
    capsys.readouterr()
    assert reports.archive("topsql", {"rows": []}, name="20261013T000000Z.time", instance="og5", now="20261013T000000Z") is not None
    monkeypatch.setattr(reports.os, "stat", real)
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    assert [e["file"] for e in idx][0] == "20261010T000000Z.time.json" and len(idx) == 4
    assert "已不在" not in capsys.readouterr().err


def test_gone_entries_do_not_count_toward_keep(monkeypatch, tmp_path):
    """清掉的条目不占 keep 的名额:否则 50 份里 28 份是空登记,轮换会把还在的老报告先删掉。"""
    d = _three_reports(monkeypatch, tmp_path)
    (d / "20261011T000000Z.time.json").unlink()          # 中间那份没了;不清掉它的空登记,轮换就会把 10 号那份删掉
    reports.archive("topsql", {"rows": []}, name="20261013T000000Z.time", instance="og5", now="20261013T000000Z", keep=3)
    names = sorted(p.name for p in d.glob("2026*.json"))
    assert names == ["20261010T000000Z.time.json", "20261012T000000Z.time.json", "20261013T000000Z.time.json"]
