"""podctl:Pod 生命周期里的确定性动作。每条都对着 spec §6.2 的一项措施。"""
import json
import os
import pathlib
import sqlite3
import sys
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "docker"))

import podctl  # noqa: E402


# ---- 渲染配置 ---------------------------------------------------------------

def test_render_opencode_config_uses_env_and_disables_share():
    env = {"MODEL_BASE_URL": "http://llm.internal/v1", "MODEL_API_KEY": "k", "MODEL_ID": "qwen3-32b"}
    cfg = podctl.render_opencode_config(env)
    assert cfg["share"] == "disabled" and cfg["autoupdate"] is False
    assert cfg["permission"]["external_directory"] == "allow"
    prov = cfg["provider"]["model-service"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"] == {"baseURL": "http://llm.internal/v1", "apiKey": "k"}
    assert "qwen3-32b" in prov["models"] and cfg["model"] == "model-service/qwen3-32b"


def test_render_opencode_config_requires_model_vars():
    with pytest.raises(podctl.ConfigError) as exc:
        podctl.render_opencode_config({"MODEL_BASE_URL": "x"})
    assert "MODEL_API_KEY" in str(exc.value) and "MODEL_ID" in str(exc.value)


_ENV = {"MODEL_BASE_URL": "u", "MODEL_API_KEY": "k", "MODEL_ID": "deepseek-flash"}


def test_model_is_text_only_unless_modalities_are_declared():
    """不设 MODEL_INPUT_MODALITIES 时与以前完全一样:opencode 按纯文本模型处理。"""
    cfg = podctl.render_opencode_config(_ENV)
    assert cfg["provider"]["model-service"]["models"]["deepseek-flash"] == {"name": "deepseek-flash"}


def test_declared_image_input_reaches_the_model_config():
    """2026-10-22:没声明时 read 在客户端就拦下图片(this model does not support image input)。"""
    cfg = podctl.render_opencode_config(dict(_ENV, MODEL_INPUT_MODALITIES="image"))
    m = cfg["provider"]["model-service"]["models"]["deepseek-flash"]
    assert m["modalities"] == {"input": ["text", "image"], "output": ["text"]}   # text 自动带上


@pytest.mark.parametrize("raw,want", [("text,image", ["text", "image"]), (" Image ; TEXT ", ["text", "image"]),
                                      ("text,image,image,pdf", ["text", "image", "pdf"]), ("", None)])
def test_modalities_are_normalized(raw, want):
    assert podctl.input_modalities({"MODEL_INPUT_MODALITIES": raw}) == want


def test_unknown_modality_fails_loudly_with_the_choices():
    with pytest.raises(podctl.ConfigError) as exc:
        podctl.render_opencode_config(dict(_ENV, MODEL_INPUT_MODALITIES="text,images"))
    assert "images" in str(exc.value) and "image" in str(exc.value) and "text,image" in str(exc.value)


def test_render_opencode_config_matches_template_keys():
    """docker/opencode.jsonc.tmpl 是给人看的;它的顶层键必须和真正渲染出来的一致,否则文档骗人。"""
    tmpl = json.loads(podctl.strip_jsonc((_ROOT / "docker" / "opencode.jsonc.tmpl").read_text(encoding="utf-8")))
    cfg = podctl.render_opencode_config({"MODEL_BASE_URL": "u", "MODEL_API_KEY": "k", "MODEL_ID": "m"})
    assert set(tmpl) == set(cfg)


def test_render_gdaa_config_api_mode(tmp_path):
    text = podctl.render_gdaa_config({"GRMP_API_HOST": "grmp.internal", "GRMP_API_PORT": "8443"})
    assert "connection_mode: api" in text
    assert "host: grmp.internal" in text and "port: 8443" in text and "token_env: GRMP_AUTH_TOKEN" in text


def test_render_gdaa_config_requires_host():
    with pytest.raises(podctl.ConfigError):
        podctl.render_gdaa_config({})


# ---- .owner 独占 --------------------------------------------------------------

def test_owner_acquire_writes_marker(tmp_path):
    assert podctl.owner_acquire(tmp_path, "runtime-u1-abc", stale_s=90, wait_s=0) is True
    data = json.loads((tmp_path / ".owner").read_text(encoding="utf-8"))
    assert data["pod"] == "runtime-u1-abc" and time.time() - data["ts"] < 5


def test_owner_acquire_refuses_fresh_marker_of_another_pod(tmp_path):
    podctl.owner_acquire(tmp_path, "runtime-u1-old", stale_s=90, wait_s=0)
    assert podctl.owner_acquire(tmp_path, "runtime-u1-new", stale_s=90, wait_s=0) is False
    assert json.loads((tmp_path / ".owner").read_text())["pod"] == "runtime-u1-old"


def test_owner_acquire_takes_over_stale_marker(tmp_path):
    (tmp_path / ".owner").write_text(json.dumps({"pod": "runtime-u1-dead", "ts": time.time() - 3600}), encoding="utf-8")
    assert podctl.owner_acquire(tmp_path, "runtime-u1-new", stale_s=90, wait_s=0) is True
    assert json.loads((tmp_path / ".owner").read_text())["pod"] == "runtime-u1-new"


def test_owner_acquire_waits_until_marker_goes_stale(tmp_path, monkeypatch):
    (tmp_path / ".owner").write_text(json.dumps({"pod": "runtime-u1-old", "ts": time.time() - 88}), encoding="utf-8")
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        (tmp_path / ".owner").write_text(json.dumps({"pod": "runtime-u1-old", "ts": time.time() - 200}), encoding="utf-8")

    monkeypatch.setattr(podctl.time, "sleep", fake_sleep)
    assert podctl.owner_acquire(tmp_path, "runtime-u1-new", stale_s=90, wait_s=55) is True
    assert sleeps == [5]


def test_owner_refresh_only_touches_own_marker(tmp_path):
    podctl.owner_acquire(tmp_path, "a", stale_s=90, wait_s=0)
    old = json.loads((tmp_path / ".owner").read_text())["ts"]
    time.sleep(0.01)
    assert podctl.owner_refresh(tmp_path, "a") is True
    assert json.loads((tmp_path / ".owner").read_text())["ts"] > old
    assert podctl.owner_refresh(tmp_path, "b") is False


def test_owner_release_only_removes_own_marker(tmp_path):
    podctl.owner_acquire(tmp_path, "a", stale_s=90, wait_s=0)
    podctl.owner_release(tmp_path, "b")
    assert (tmp_path / ".owner").exists()
    podctl.owner_release(tmp_path, "a")
    assert not (tmp_path / ".owner").exists()


# ---- SQLite:自检 / checkpoint / 备份 ---------------------------------------

def _make_db(path: pathlib.Path, rows: int = 3) -> None:
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("create table t(x)")
    con.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


def test_db_check_passes_on_healthy_db(tmp_path, capsys):
    db = tmp_path / "opencode.db"
    _make_db(db)
    assert podctl.db_check(db, tmp_path / "backup") == "ok"
    assert "完整性检查通过" in capsys.readouterr().out


def test_db_check_missing_db_is_first_start(tmp_path, capsys):
    assert podctl.db_check(tmp_path / "opencode.db", tmp_path / "backup") == "absent"
    assert "首次启动" in capsys.readouterr().out


def test_db_check_restores_from_latest_backup(tmp_path, capsys):
    db = tmp_path / "opencode.db"
    bdir = tmp_path / "backup"
    bdir.mkdir()
    _make_db(bdir / "opencode.db.20260101T000000Z", rows=1)
    _make_db(bdir / "opencode.db.20260102T000000Z", rows=7)
    db.write_bytes(b"this is not a sqlite database at all" * 100)
    assert podctl.db_check(db, bdir) == "restored"
    out = capsys.readouterr().out
    assert "20260102T000000Z" in out and "损坏" in out
    assert list(tmp_path.glob("opencode.db.corrupt-*"))
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from t").fetchone()[0] == 7
    con.close()


def test_db_check_without_backup_starts_empty_and_says_so(tmp_path, capsys):
    db = tmp_path / "opencode.db"
    db.write_bytes(b"garbage" * 100)
    assert podctl.db_check(db, tmp_path / "backup") == "reset"
    assert not db.exists() and "没有可用备份" in capsys.readouterr().out
    assert list(tmp_path.glob("opencode.db.corrupt-*"))


def test_backup_list_ignores_wal_and_shm_sidecars(tmp_path):
    """**备份清单只认真正的备份文件。**

    `opencode.db.*` 这个通配同时命中 `opencode.db.<时间戳>-wal` 与 `-shm` ——
    而 `-wal` 按字典序排在同名备份**之后**,于是「取最新一份」取到的是那个侧车文件。
    侧车常常是 0 字节,而 SQLite 把 0 字节文件当成一个合法的空库:
    自检于是打印「已从备份 …-wal 恢复」并返回 restored,用户的历史却全空了。
    真实形态:恢复成功的日志 + 空的对话列表,没有任何报错。
    """
    bdir = tmp_path / "backup"
    bdir.mkdir()
    _make_db(bdir / "opencode.db.20260102T000000Z", rows=7)
    (bdir / "opencode.db.20260102T000000Z-wal").write_bytes(b"")
    (bdir / "opencode.db.20260102T000000Z-shm").write_bytes(b"")
    (bdir / "opencode.db.corrupt-20260102T000000Z").write_bytes(b"garbage")

    names = [p.name for p in podctl._backups(bdir)]
    assert names == ["opencode.db.20260102T000000Z"], (
        "备份清单里混进了非备份文件:%s" % names)


def test_rotation_counts_only_real_backups(tmp_path):
    """轮转也走同一个清单。侧车被算进「份数」时,keep=3 实际留下的真备份不足 3 份。"""
    db = tmp_path / "opencode.db"
    _make_db(db)
    bdir = tmp_path / "backup"
    for i in range(1, 6):
        podctl.db_backup(db, bdir, keep=3, now="2026010%dT000000Z" % i)
    kept = [p.name for p in podctl._backups(bdir)]
    assert len(kept) == 3, "应留 3 份真备份,实际 %s" % kept
    assert kept == ["opencode.db.20260103T000000Z",
                    "opencode.db.20260104T000000Z",
                    "opencode.db.20260105T000000Z"]


def test_empty_file_is_not_a_usable_backup(tmp_path):
    """0 字节文件能被 SQLite 正常打开,`integrity_check` 也回 ok ——
    拿它当备份恢复,等于用一个空库覆盖并宣告成功。"""
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert not podctl._integrity_ok(empty)


def test_db_checkpoint_truncates_wal(tmp_path):
    db = tmp_path / "opencode.db"
    _make_db(db, rows=50)
    con = sqlite3.connect(db)
    con.execute("insert into t values (99)")
    con.commit()                                  # 留着连接:最后一个连接关闭时 SQLite 会自己删 -wal,那不是我们要测的
    wal = db.with_name("opencode.db-wal")
    assert wal.stat().st_size > 0
    podctl.db_checkpoint(db)                      # 另开连接做 TRUNCATE checkpoint
    assert wal.stat().st_size == 0
    con.close()


def test_db_backup_rotates(tmp_path):
    db = tmp_path / "opencode.db"
    _make_db(db)
    bdir = tmp_path / "backup"
    for i in range(5):
        p = podctl.db_backup(db, bdir, keep=3, now=f"2026010{i+1}T000000Z")
        assert p.name == f"opencode.db.2026010{i+1}T000000Z"
    names = sorted(x.name for x in bdir.iterdir())
    assert names == ["opencode.db.20260103T000000Z", "opencode.db.20260104T000000Z", "opencode.db.20260105T000000Z"]
    con = sqlite3.connect(bdir / names[-1])
    assert con.execute("select count(*) from t").fetchone()[0] == 3
    con.close()


def test_log_prune_removes_old_logs_only(tmp_path):
    old = tmp_path / "a.log"
    old.write_text("x")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    new = tmp_path / "b.log"
    new.write_text("x")
    other = tmp_path / "keep.txt"
    other.write_text("x")
    os.utime(other, (time.time() - 10 * 86400,) * 2)
    assert podctl.log_prune(tmp_path, days=7) == [old]
    assert not old.exists() and new.exists() and other.exists()


# ---- 命令行 -----------------------------------------------------------------

def test_cli_render_writes_files_and_reports_missing_vars(tmp_path, monkeypatch, capsys):
    for k in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_ID", "GRMP_API_HOST"):
        monkeypatch.delenv(k, raising=False)
    assert podctl.main(["render-opencode-config", "--out", str(tmp_path / "oc.json")]) == 2
    assert "MODEL_ID" in capsys.readouterr().err
    monkeypatch.setenv("MODEL_BASE_URL", "u")
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_ID", "m")
    monkeypatch.setenv("GRMP_API_HOST", "h")
    assert podctl.main(["render-opencode-config", "--out", str(tmp_path / "oc.json")]) == 0
    assert podctl.main(["render-gdaa-config", "--out", str(tmp_path / "config.yaml")]) == 0
    assert json.loads((tmp_path / "oc.json").read_text())["share"] == "disabled"
    assert oct((tmp_path / "oc.json").stat().st_mode & 0o777) == "0o600"       # 里面有 key


def test_render_gdaa_config_appkey_requires_private_key():
    """2026-09-18 中间件加固:平台配了 GRMP_APPKEY 却没注入 SM2 私钥,启动时就拒——
    否则表现成每个请求被中间件拒,排查方向被带到中间件那边。"""
    env = {"GRMP_API_HOST": "grmp.internal", "GRMP_APPKEY": "gaussdb-agent"}
    with pytest.raises(podctl.ConfigError) as info:
        podctl.render_gdaa_config(env)
    assert "GRMP_SM2_PRIVATE_KEY" in str(info.value) and "grmp-sm2" in str(info.value)
    text = podctl.render_gdaa_config({**env, "GRMP_SM2_PRIVATE_KEY": "ab" * 32})
    assert "appkey_env: GRMP_APPKEY" in text
    assert "appkey_env" not in podctl.render_gdaa_config({"GRMP_API_HOST": "grmp.internal"})   # 没开签名不写这行


def test_render_gdaa_config_behind_the_credproxy_needs_no_private_key():
    """2026-10-23 凭据代理:私钥在代理那边,opencode 的容器里本来就没有。"""
    env = {"GRMP_API_HOST": "grmp.internal", "GRMP_APPKEY": "gaussdb-agent",
           "GRMP_PROXY_SOCKET": "/run/credproxy/credproxy.sock"}
    assert "appkey_env: GRMP_APPKEY" in podctl.render_gdaa_config(env)


# ---- 凭据代理与工具权限(2026-10-23) ------------------------------------------------

def test_behind_the_credproxy_the_model_key_never_reaches_opencode_json():
    env = {"MODEL_PROXY_URL": "http://127.0.0.1:4099/model", "MODEL_ID": "m",
           "MODEL_BASE_URL": "http://llm.internal/v1", "MODEL_API_KEY": "real-key"}
    cfg = podctl.render_opencode_config(env)
    assert cfg["provider"]["model-service"]["options"] == {"baseURL": "http://127.0.0.1:4099/model",
                                                           "apiKey": "credproxy"}
    assert "real-key" not in json.dumps(cfg) and "llm.internal" not in json.dumps(cfg)
    assert podctl.render_opencode_config({"MODEL_PROXY_URL": "http://p/model", "MODEL_ID": "m"})   # 只要 MODEL_ID
    with pytest.raises(podctl.ConfigError) as exc:
        podctl.render_opencode_config({"MODEL_PROXY_URL": "http://p/model"})
    assert "MODEL_ID" in str(exc.value) and "MODEL_API_KEY" not in str(exc.value)


def test_permissions_by_role():
    run = podctl.opencode_permissions({})
    kb = podctl.opencode_permissions({"AGENT_ROLE": "kb-import"})
    assert run["edit"] == "deny" and kb["edit"] == "allow"          # kb-init 用 write 工具写草稿
    for p in (run, kb):
        assert p["webfetch"] == p["websearch"] == "deny"
        assert p["skill"] == {"*": "deny", "gaussdb-*": "allow"}
        assert p["external_directory"] == "allow" and p["read"] == "allow"
        assert p["bash"] == run["bash"]
    with pytest.raises(podctl.ConfigError):
        podctl.opencode_permissions({"AGENT_ROLE": "admin"})
    assert podctl.render_opencode_config(dict(_ENV, AGENT_ROLE="kb-import"))["permission"]["edit"] == "allow"


def _opencode_decides(rules: dict, command: str) -> str:
    """opencode 1.18.27 的口径:规则按写的顺序,最后一条匹配的生效;`*` 匹配任意字符(含 `/` 与空格),
    `?` 一个字符,整条命令锚定;「x *」也匹配不带参数的「x」(所以白名单里两种都写了,这里照样算)。"""
    import re
    verdict = "ask"
    for pat, action in rules.items():
        rx = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pat)
        if pat.endswith(" *"):
            rx = rx[:-len(re.escape(" ") + ".*")] + "( .*)?"
        if re.fullmatch(rx, command, re.S):
            verdict = action
    return verdict


_SKILLS = "/data/config/opencode/skills"


@pytest.mark.parametrize("cmd", [
    "python3 %s/gaussdb-login/scripts/login.py --list" % _SKILLS,
    "python3 %s/gaussdb-wdr/scripts/wdr.py --instance a --since 1h" % _SKILLS,
    "python %s/gaussdb-kb/scripts/kb.py query 锁等待" % _SKILLS,
    "cat /data/reports/health/latest.json", "ls", "ls -la /nas/kb", "grep -n lock /nas/kb/cases/a.md",
    "head -50 x.md", "date", "pwd", "wc -l a b",
    # 2026-10-24 测试环境导入工单时被拦的三条:无害的重定向结尾、find
    "ls /data/config/opencode/skills/ 2>/dev/null", "ls -la /data/state/workspace 2>/dev/null",
    'find /nas/kb/inbox -type f -newermt "2026-10-24 00:00" 2>/dev/null',
    "python3 %s/gaussdb-health/scripts/health.py --session abc 2>&1" % _SKILLS,
    "python3 %s/gaussdb-kb/scripts/kb.py health > /dev/null" % _SKILLS, "ls 2>/dev/null", "cat a.md 2> /dev/null",
    "cd /nas/kb", "stat /nas/kb/kb.yaml", "which python3", "true", "find /nas/kb -name '*.md'",
])
def test_bash_allows_skill_scripts_and_read_only_commands(cmd):
    assert _opencode_decides(podctl.opencode_permissions({})["bash"], cmd) == "allow", cmd


@pytest.mark.parametrize("cmd", [
    "env", "printenv", "printenv GRMP_AUTH_TOKEN", "set", "export", "declare -p", "env | grep KEY",
    "cat /proc/1/environ", "cat /proc/self/environ", "tr '\\0' '\\n' < /proc/1/environ", "grep -a TOKEN /proc/7/environ",
    "python3 -c 'import os; print(os.environ)'", "python3 /tmp/x.py", "python3 -m http.server",
    "python3 %s/gaussdb-x/../../../../tmp/evil.py" % _SKILLS, "python3 %s/../../../tmp/evil.py" % _SKILLS,
    "cat > /tmp/x.py", "echo 'import os' > %s/gaussdb-x/scripts/e.py" % _SKILLS, "echo x >> a",
    "curl http://example.com", "wget http://x", "bash -c env", "sh -c id", "node -e 1", "rm -rf /data",
    "cp a b", "mv a b", "chmod +x a", "kill -9 1", "nc 10.0.0.1 80", "perl -e 1",
    # 借安全结尾夹带:写文件、读环境、跑不在名单里的命令,都不能因为结尾是 2>/dev/null 就放行
    "cat > /tmp/x.py 2>/dev/null", "echo 'import os' > %s/gaussdb-x/e.py 2>&1" % _SKILLS,
    "cat /proc/1/environ 2>/dev/null", "env 2>/dev/null", "printenv 2>&1", "curl http://x 2>/dev/null",
    "python3 -c 'print(1)' 2>&1", "ls > /tmp/x", "ls x > /dev/null 2>&1",
    "python3 %s/gaussdb-x/s.py > /tmp/out.txt" % _SKILLS, "ls 2>/tmp/err.txt",
    "find / -name x -delete", "find . -exec cat {} ;", "find . -fprint /tmp/list 2>/dev/null",
])
def test_bash_denies_env_reads_writes_and_anything_else(cmd):
    assert _opencode_decides(podctl.opencode_permissions({})["bash"], cmd) == "deny", cmd


def test_skill_path_follows_xdg_config_home():
    rules = podctl.opencode_permissions({"XDG_CONFIG_HOME": "/pool/cfg/"})["bash"]
    assert _opencode_decides(rules, "python3 /pool/cfg/opencode/skills/gaussdb-a/scripts/a.py") == "allow"
    assert _opencode_decides(rules, "python3 %s/gaussdb-a/scripts/a.py" % _SKILLS) == "deny"
    assert list(rules)[0] == "*"                       # 先全拒:最后一条匹配的生效


# ---- 报告只读端口 ----------------------------------------------------------------

import http.client
import threading


def _serve(tmp_path):
    root = tmp_path / "reports"
    (root / "health").mkdir(parents=True)
    (root / "health" / "latest.json").write_text('{"overall": 2}', encoding="utf-8")
    (root / "kb").mkdir()
    (root / "kb" / "queries.jsonl").write_text('{"q":"a"}\n', encoding="utf-8")
    (root / "wdr").mkdir()
    (root / "wdr" / "x.native.html").write_text("<html>", encoding="utf-8")
    (root / "health" / "secret.txt").write_text("no", encoding="utf-8")
    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    srv = podctl.make_reports_server(root, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _get(port, path, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, body


def test_reports_server_serves_json_jsonl_html_with_no_store(tmp_path):
    srv, port = _serve(tmp_path)
    try:
        st, h, body = _get(port, "/health/latest.json")
        assert st == 200 and body == b'{"overall": 2}'
        assert h["content-type"].startswith("application/json") and h["cache-control"] == "no-store"
        assert _get(port, "/kb/queries.jsonl")[0] == 200
        assert _get(port, "/wdr/x.native.html")[1]["content-type"].startswith("text/html")
    finally:
        srv.shutdown(); srv.server_close()


def test_reports_server_refuses_traversal_other_suffixes_dirs_and_non_get(tmp_path):
    """**这是安全边界**:这个端口只能读本人 reports/ 下三种文件,别的一律 404。"""
    srv, port = _serve(tmp_path)
    try:
        assert _get(port, "/../outside.json")[0] == 404
        assert _get(port, "/health/%2e%2e/%2e%2e/outside.json")[0] == 404
        assert _get(port, "/health/secret.txt")[0] == 404
        assert _get(port, "/health/")[0] == 404 and _get(port, "/")[0] == 404
        assert _get(port, "/health/nope.json")[0] == 404
        assert _get(port, "/health/latest.json", method="POST")[0] == 405
    finally:
        srv.shutdown(); srv.server_close()


def test_serve_reports_subcommand_is_wired(monkeypatch, tmp_path):
    """子命令要能从 CLI 到达 make_reports_server —— 不然 entrypoint 里那行是死的。"""
    called = {}

    class _Srv:
        def serve_forever(self):
            called["ran"] = True

    monkeypatch.setattr(podctl, "make_reports_server",
                        lambda root, port, user_id="", kb_dir=None, token="": called.setdefault("args", (root, port)) and _Srv())
    assert podctl.main(["serve-reports", "--dir", str(tmp_path), "--port", "4097"]) == 0
    assert called["args"] == (tmp_path, 4097) and called["ran"]


def test_reports_server_whoami_comes_from_env_not_from_files(tmp_path):
    """大盘侧栏要显示工号。它来自 GSDB_USER_ID(平台按 Pod 注入),不是目录里的文件 ——
    目录里的东西用户自己能改,工号不能让他改。"""
    root = tmp_path / "reports"
    root.mkdir()
    srv = podctl.make_reports_server(root, 0, user_id="u1234")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st, h, body = _get(srv.server_address[1], "/whoami.json")
        assert st == 200 and json.loads(body) == {"user_id": "u1234"} and h["cache-control"] == "no-store"
        assert not (root / "whoami.json").exists(), "不是文件,是环境变量"
    finally:
        srv.shutdown(); srv.server_close()


def test_reports_server_kb_catalog_only_when_kb_dir_given(tmp_path):
    """/_kb/catalog.json:大盘知识库三个页签的数据,从共享知识库现读现算(不落盘)。
    没给 --kb-dir 时 404 —— 端口不会因为多了这条路而默认去读知识库。"""
    root = tmp_path / "reports"
    root.mkdir()
    kb = tmp_path / "kb"
    (kb / "rules").mkdir(parents=True)
    (kb / "rules" / "ops.yaml").write_text("# 运维规范\n- id: GS-OPS-001\n  severity: error\n  rule: 灌数后必须 ANALYZE\n",
                                           encoding="utf-8")
    (kb / "inbox").mkdir()
    (kb / "inbox" / "a.md").write_text("未审核", encoding="utf-8")
    srv = podctl.make_reports_server(root, 0, kb_dir=kb)
    bare = podctl.make_reports_server(root, 0)
    for s in (srv, bare):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        st, h, body = _get(srv.server_address[1], "/_kb/catalog.json")
        d = json.loads(body)
        assert st == 200 and h["cache-control"] == "no-store" and h["content-type"].startswith("application/json")
        assert d["groups"][0]["rules"][0]["id"] == "GS-OPS-001"
        assert _get(bare.server_address[1], "/_kb/catalog.json")[0] == 404
        # 知识库里的文件本身仍然不能经这个端口直接读
        assert _get(srv.server_address[1], "/_kb/inbox/a.md")[0] == 404
        assert _get(srv.server_address[1], "/_kb/rules/ops.yaml")[0] == 404
    finally:
        srv.shutdown(); bare.shutdown()


def test_serve_reports_passes_kb_dir(monkeypatch, tmp_path):
    called = {}

    class _Srv:
        def serve_forever(self):
            pass

    monkeypatch.setattr(podctl, "make_reports_server",
                        lambda root, port, user_id="", kb_dir=None, token="": called.setdefault("kb", kb_dir) and _Srv() or _Srv())
    assert podctl.main(["serve-reports", "--dir", str(tmp_path), "--port", "4097", "--kb-dir", str(tmp_path / "kb")]) == 0
    assert called["kb"] == tmp_path / "kb"


def test_serve_reports_takes_the_token_from_env_not_argv(monkeypatch, tmp_path):
    """固定池化:报告令牌经环境变量传 —— 命令行同容器里 ps 看得到。"""
    called = {}

    class _Srv:
        def serve_forever(self):
            pass

    monkeypatch.setenv("REPORTS_TOKEN", "t" * 32)
    monkeypatch.setattr(podctl, "make_reports_server",
                        lambda root, port, user_id="", kb_dir=None, token="": called.setdefault("token", token) and _Srv())
    assert podctl.main(["serve-reports", "--dir", str(tmp_path), "--port", "4097"]) == 0
    assert called["token"] == "t" * 32


def test_reports_server_with_a_token_refuses_everything_without_it(tmp_path):
    """同一个 Pod 的 8 个槽位共用 localhost:有令牌时每条路(含 whoami 与知识库目录)都要带 Bearer。"""
    root = tmp_path / "reports"
    root.mkdir()
    (root / "r.html").write_text("<p>x</p>", encoding="utf-8")
    srv = podctl.make_reports_server(root, 0, user_id="u1234", token="t" * 32)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        for path in ("/whoami.json", "/r.html", "/_kb/catalog.json", "/nope.html"):
            assert _get(port, path)[0] == 401, path
        assert _get(port, "/whoami.json", headers={"Authorization": "Bearer " + "x" * 32})[0] == 401
        st, _h, body = _get(port, "/whoami.json", headers={"Authorization": "Bearer " + "t" * 32})
        assert st == 200 and json.loads(body) == {"user_id": "u1234"}
    finally:
        srv.shutdown()


# ---- 实例别名(api 模式) ------------------------------------------------------
# 2026-10-16 测试时模型每次都问「og5 是哪个库」:api 模式没有预置连接清单,而登录按对话隔离不能放松。
# 部署时登记「别名 → 实例 IP / 库名」,login --list 能列、--alias 能登。写错一行不能让 Pod 起不来
# (只影响这一个功能),但也不能悄悄丢掉 —— 错误原文写进配置,login --list 时说出来。

def test_instance_aliases_are_rendered_into_the_api_config():
    import yaml
    env = {"GRMP_API_HOST": "grmp.internal",
           "GRMP_INSTANCE_ALIASES": "og5 = 10.0.0.9 / postgres   # 测试库\ncore=10.1.2.3/coredb; rpt = 10.1.2.4/report"}
    cfg = yaml.safe_load(podctl.render_gdaa_config(env))
    assert cfg["instance_aliases"] == [
        {"name": "og5", "ip": "10.0.0.9", "database": "postgres", "note": "测试库"},
        {"name": "core", "ip": "10.1.2.3", "database": "coredb", "note": ""},
        {"name": "rpt", "ip": "10.1.2.4", "database": "report", "note": ""}]
    assert "instance_aliases_errors" not in cfg


def test_bad_alias_lines_are_kept_as_errors_not_dropped_and_not_fatal():
    import yaml
    env = {"GRMP_API_HOST": "grmp.internal",
           "GRMP_INSTANCE_ALIASES": "og5=10.0.0.9/postgres\nbroken line\nog5=10.0.0.10/x\nbad name!=1.2.3.4/db"}
    cfg = yaml.safe_load(podctl.render_gdaa_config(env))
    assert [a["name"] for a in cfg["instance_aliases"]] == ["og5"]
    errs = cfg["instance_aliases_errors"]
    assert len(errs) == 3 and any("broken line" in e for e in errs) and any("重复" in e for e in errs)


def test_no_aliases_means_no_alias_keys():
    import yaml
    cfg = yaml.safe_load(podctl.render_gdaa_config({"GRMP_API_HOST": "grmp.internal", "GRMP_INSTANCE_ALIASES": "  "}))
    assert "instance_aliases" not in cfg and "instance_aliases_errors" not in cfg
