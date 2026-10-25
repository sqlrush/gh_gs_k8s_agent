"""common.kb.embed —— 用本地假 embeddings 服务钉住:归位、校验、批隔离、缓存、超时。"""
import hashlib
import json
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import config as kbconfig, embed  # noqa: E402

DIMS = 4


def _vec(text: str):
    h = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in h[:DIMS]]


class _Fake(BaseHTTPRequestHandler):
    """特殊输入:FAIL → 500;SLOW → 睡过超时;BADDIM → 维度错;SHUFFLE 在服务端乱序返回。"""
    requests = []
    auth_headers = []

    def log_message(self, *a):        # 安静
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        _Fake.requests.append(body)
        _Fake.auth_headers.append(self.headers.get("Authorization"))
        texts = body["input"]
        if any(t == "FAIL" for t in texts):
            self.send_response(500); self.end_headers(); self.wfile.write(b"boom"); return
        if any(t == "SLOW" for t in texts):
            time.sleep(1.5)
        data = []
        for i, t in enumerate(texts):
            v = _vec(t)
            if t == "BADDIM":
                v = v[:2]
            data.append({"index": i, "embedding": v})
        data.reverse()                # 服务端乱序,客户端必须按 index 归位
        out = json.dumps({"data": data}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(out)


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture
def emb(server, tmp_path):
    _Fake.requests.clear(); _Fake.auth_headers.clear()
    return embed.Embedder(server, "sk-test", "fake", DIMS, timeout_s=0.8, batch=2, cache_dir=tmp_path / "cache")


# ---------------------------------------------------------------- parse (pure)

def test_parse_reorders_by_index():
    payload = {"data": [{"index": 1, "embedding": [1, 1, 1, 1]}, {"index": 0, "embedding": [0, 0, 0, 0]}]}
    assert embed.parse_embeddings(payload, 2, 4) == [[0, 0, 0, 0], [1, 1, 1, 1]]


@pytest.mark.parametrize("payload", [
    {"data": [{"index": 0, "embedding": [0, 0, 0, 0]}]},                                   # 少一条
    {"data": [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 0, 0, 0]}]},  # 维度错
    {"data": [{"index": 0, "embedding": [0, 0, 0, 0]}, {"index": 0, "embedding": [0, 0, 0, 0]}]},  # index 重复
    {"data": [{"index": 0, "embedding": [0, 0, 0, float("nan")]}, {"index": 1, "embedding": [0, 0, 0, 0]}]},
    {"error": "x"},
])
def test_parse_rejects_bad_payloads(payload):
    with pytest.raises(ValueError):
        embed.parse_embeddings(payload, 2, 4)


# ---------------------------------------------------------------- embed

def test_embed_returns_vectors_in_input_order(emb):
    got = emb.embed(["a", "b", "c"])
    assert got == [_vec("a"), _vec("b"), _vec("c")]
    assert emb.stats.embedded == 3 and emb.stats.failed == 0


def test_embed_sends_bearer_and_batches(emb):
    emb.embed(["a", "b", "c"])
    assert _Fake.auth_headers[0] == "Bearer sk-test"
    assert [len(r["input"]) for r in _Fake.requests] == [2, 1]


def test_failed_batch_is_isolated(emb):
    got = emb.embed(["a", "FAIL", "c", "d"])       # batch=2:[a,FAIL] 失败,[c,d] 成功
    assert got[0] is None and got[1] is None
    assert got[2] == _vec("c") and got[3] == _vec("d")
    assert emb.stats.failed == 2 and emb.stats.embedded == 2
    assert "500" in emb.stats.last_error


def test_timeout_yields_none_not_exception(emb):
    got = emb.embed(["SLOW"])
    assert got == [None]
    assert emb.stats.failed == 1


def test_bad_dims_from_server_is_a_failed_batch(emb):
    assert emb.embed(["BADDIM"]) == [None]


def test_cache_hit_skips_the_request(emb):
    emb.embed(["a"])
    n = len(_Fake.requests)
    assert emb.embed(["a"]) == [_vec("a")]
    assert len(_Fake.requests) == n
    assert emb.stats.cached == 1 and emb.stats.embedded == 0


def test_cache_is_keyed_by_model(emb, server, tmp_path):
    emb.embed(["a"])
    other = embed.Embedder(server, "", "other-model", DIMS, cache_dir=tmp_path / "cache")
    n = len(_Fake.requests)
    other.embed(["a"])
    assert len(_Fake.requests) == n + 1


def test_repr_hides_key(emb):
    assert "sk-test" not in repr(emb)


def test_bypasses_http_proxy(emb, monkeypatch):
    """开发机上系统代理把 127.0.0.1 都劫走过(返回 503)——客户端必须无视代理。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert emb.embed(["proxy-check"]) == [_vec("proxy-check")]


# ---------------------------------------------------------------- from_config

def test_from_config_none_source_gives_none(tmp_path):
    cfg = kbconfig.load(tmp_path)
    assert embed.Embedder.from_config(cfg) is None


def test_from_config_url_source_reads_key_env(tmp_path, monkeypatch):
    (tmp_path / "kb.yaml").write_text(
        "embeddings: {source: url, base_url: http://h/v1, model: m, dims: 8, api_key_env: K}\n", encoding="utf-8")
    monkeypatch.setenv("K", "sk-from-env")
    e = embed.Embedder.from_config(kbconfig.load(tmp_path))
    assert e.base_url == "http://h/v1" and e.dims == 8 and e._api_key == "sk-from-env"
    assert e.cache_dir == tmp_path / "index" / "embed-cache"
