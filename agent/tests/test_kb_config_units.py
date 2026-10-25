"""common.kb.config —— kb.yaml、OpenCode provider 端点、凭据名解析(无库)。

三条红线用测试钉死:
  · 密钥只在内存里,repr/str/日志里绝不出现;
  · 解析 OpenCode 的 jsonc 时,字符串里的 `//`(URL)不能被当注释砍掉;
  · 没有 kb.yaml 也要能得到一套可用的默认配置(客户零配置时词法+图仍能跑)。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import config as kbconfig  # noqa: E402


# ---------------------------------------------------------------- jsonc

def test_strip_jsonc_removes_line_and_block_comments():
    src = '{\n  // 行注释\n  "a": 1, /* 块 */ "b": 2\n}'
    assert json.loads(kbconfig.strip_jsonc(src)) == {"a": 1, "b": 2}


def test_strip_jsonc_keeps_double_slash_inside_strings():
    src = '{"baseURL": "https://api.kimi.com/coding/v1" // 注释\n}'
    assert json.loads(kbconfig.strip_jsonc(src))["baseURL"] == "https://api.kimi.com/coding/v1"


def test_strip_jsonc_handles_escaped_quotes_in_strings():
    src = '{"s": "a \\" // not a comment", "n": 1}'
    assert json.loads(kbconfig.strip_jsonc(src)) == {"s": 'a " // not a comment', "n": 1}


def test_strip_jsonc_tolerates_trailing_commas():
    src = '{"a": [1, 2,], "b": {"c": 1,},}'
    assert json.loads(kbconfig.strip_jsonc(src)) == {"a": [1, 2], "b": {"c": 1}}


# ---------------------------------------------------------------- opencode provider

def _write_opencode(tmp_path, body: str, auth: dict = None):
    cfg_dir = tmp_path / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(body, encoding="utf-8")
    if auth is not None:
        auth_dir = tmp_path / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        (auth_dir / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    return tmp_path


_OPENCODE = """{
  // 开发机同款形状
  "model": "kimi/k3",
  "provider": {
    "kimi": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://api.kimi.com/coding/v1", "apiKey": "sk-inline-secret" }
    }
  }
}"""


def test_opencode_provider_resolves_base_url_and_inline_key(tmp_path, monkeypatch):
    home = _write_opencode(tmp_path, _OPENCODE)
    monkeypatch.setenv("HOME", str(home))
    ep = kbconfig.opencode_provider()
    assert ep.base_url == "https://api.kimi.com/coding/v1"
    assert ep.provider == "kimi"
    assert ep.api_key == "sk-inline-secret"


def test_provider_endpoint_never_leaks_key_in_repr(tmp_path, monkeypatch):
    home = _write_opencode(tmp_path, _OPENCODE)
    monkeypatch.setenv("HOME", str(home))
    ep = kbconfig.opencode_provider()
    for shown in (repr(ep), str(ep)):
        assert "sk-inline-secret" not in shown
        assert "kimi" in shown


def test_opencode_provider_resolves_env_template(tmp_path, monkeypatch):
    body = _OPENCODE.replace('"sk-inline-secret"', '"{env:MY_KEY}"')
    home = _write_opencode(tmp_path, body)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_KEY", "from-env")
    assert kbconfig.opencode_provider().api_key == "from-env"


def test_opencode_provider_falls_back_to_auth_json(tmp_path, monkeypatch):
    body = _OPENCODE.replace(', "apiKey": "sk-inline-secret"', "")
    home = _write_opencode(tmp_path, body, auth={"kimi": {"type": "api", "key": "from-auth"}})
    monkeypatch.setenv("HOME", str(home))
    assert kbconfig.opencode_provider().api_key == "from-auth"


def test_opencode_provider_missing_config_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(kbconfig.KbConfigError, match="opencode"):
        kbconfig.opencode_provider()


def test_opencode_provider_without_base_url_is_an_error(tmp_path, monkeypatch):
    body = _OPENCODE.replace('"baseURL": "https://api.kimi.com/coding/v1", ', "")
    home = _write_opencode(tmp_path, body)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(kbconfig.KbConfigError, match="baseURL"):
        kbconfig.opencode_provider()


# ---------------------------------------------------------------- kb.yaml

def test_load_without_kb_yaml_gives_usable_defaults(tmp_path):
    cfg = kbconfig.load(tmp_path)
    assert cfg.kb_dir == tmp_path
    assert cfg.embeddings.source == "none"           # 没配 = 不假装有向量
    assert cfg.store.legacy_pg is False and cfg.store.graph is None
    assert cfg.thresholds.clause > 0 and cfg.thresholds.case > 0


def test_load_reads_store_and_embeddings(tmp_path):
    (tmp_path / "kb.yaml").write_text(
        "store:\n"
        "  pg: {host: 127.0.0.1, port: 5439, database: kb, user: kb, credential: kb-og7}\n"
        "  graph: {url: http://127.0.0.1:7474, user: neo4j, credential: kb-graph}\n"
        "embeddings: {source: opencode, model: bge-m3, dims: 1024}\n",
        encoding="utf-8")
    cfg = kbconfig.load(tmp_path)
    # store.pg 已下线(2026-10-23 改为 index/store.sqlite):留着不报错,只记一笔给 health / index 提示
    assert cfg.store.legacy_pg is True
    assert cfg.store.graph.url == "http://127.0.0.1:7474"
    assert cfg.store.graph.database == "neo4j"       # 默认库名
    assert cfg.embeddings.source == "opencode" and cfg.embeddings.dims == 1024


def test_load_rejects_plaintext_password_in_kb_yaml(tmp_path):
    """口令只能在 credentials/*.enc,kb.yaml 会被 cat、进备份——写了就直接报错。"""
    (tmp_path / "kb.yaml").write_text(
        "store:\n  pg: {host: h, port: 5432, database: d, user: u, password: oops}\n",
        encoding="utf-8")
    with pytest.raises(kbconfig.KbConfigError, match="password"):
        kbconfig.load(tmp_path)


def test_load_rejects_unknown_embedding_source(tmp_path):
    (tmp_path / "kb.yaml").write_text("embeddings: {source: magic}\n", encoding="utf-8")
    with pytest.raises(kbconfig.KbConfigError, match="source"):
        kbconfig.load(tmp_path)


def test_url_source_requires_base_url(tmp_path):
    (tmp_path / "kb.yaml").write_text("embeddings: {source: url, model: m}\n", encoding="utf-8")
    with pytest.raises(kbconfig.KbConfigError, match="base_url"):
        kbconfig.load(tmp_path)


# ---------------------------------------------------------------- kb dir resolution

@pytest.mark.parametrize("script, want", [
    ("/Users/x/.config/opencode/skills/gaussdb-kb/scripts/kb.py", "/Users/x/.config/opencode"),
    ("/Users/x/.config/opencode/skills/common/kb/config.py", "/Users/x/.config/opencode"),
    ("/Users/x/proj/.opencode/skills/gaussdb-kb/scripts/kb.py", "/Users/x/proj/.opencode"),
    ("/Users/x/opencode_skill/common/kb/config.py", "/Users/x/opencode_skill"),
])
def test_install_root_from_both_layouts(script, want):
    """common/ 装好后在 skills/ 下面,源码仓里在仓库根——两种布局都要推出同一个根。"""
    assert kbconfig.install_root(pathlib.Path(script)) == pathlib.Path(want)


def test_resolve_kb_dir_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("GSDB_KB_DIR", str(tmp_path / "env"))
    assert kbconfig.resolve_kb_dir(str(tmp_path / "cli")) == tmp_path / "cli"
    assert kbconfig.resolve_kb_dir(None) == tmp_path / "env"
    monkeypatch.delenv("GSDB_KB_DIR")
    assert kbconfig.resolve_kb_dir(None).name == "kb"
