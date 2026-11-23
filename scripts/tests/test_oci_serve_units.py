"""scripts/oci-serve.py:只读最小镜像仓库(Fuyao 模板构建用,2026-11-17)。

要点:按标签取清单时,多平台索引选出本架构的单平台清单;按摘要取清单与层;摘要格式不对(含路径穿越)一律 404。
"""
import hashlib
import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("oci_serve", _ROOT / "scripts" / "oci-serve.py")
oci = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oci)


def _blob(root, data):
    d = hashlib.sha256(data).hexdigest()
    (root / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    (root / "blobs" / "sha256" / d).write_bytes(data)
    return "sha256:" + d


@pytest.fixture()
def layout(tmp_path):
    layer = _blob(tmp_path, b"layer-bytes" * 100)
    cfg = _blob(tmp_path, b'{"architecture":"arm64","os":"linux"}')
    man = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                      "config": {"digest": cfg, "size": 1}, "layers": [{"digest": layer, "size": 1100}]}).encode()
    man_d = _blob(tmp_path, man)
    idx = json.dumps({"schemaVersion": 2, "mediaType": oci.INDEX_TYPES[0], "manifests": [
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + "0" * 64, "size": 1,
         "platform": {"os": "linux", "architecture": "amd64"}},          # 目录里没有 amd64 的清单
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": man_d, "size": len(man),
         "platform": {"os": "linux", "architecture": "arm64"}}]}).encode()
    idx_d = _blob(tmp_path, idx)
    (tmp_path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [
        {"mediaType": oci.INDEX_TYPES[0], "digest": idx_d, "size": len(idx), "annotations": {
            "io.containerd.image.name": "ghcr.io/org/gaussdb-agent-runtime:v1",
            "org.opencontainers.image.ref.name": "v1"}}]}))
    srv = oci.ThreadingHTTPServer(("127.0.0.1", 0), oci.handler(oci.Layout(str(tmp_path), "arm64")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1], man, man_d, layer
    srv.shutdown()
    srv.server_close()


def _get(url, method="GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


def test_the_tag_resolves_to_this_architecture_and_blobs_are_served(layout):
    base, man, man_d, layer = layout
    assert _get(base + "/v2/")[0] == 200
    code, h, body = _get(base + "/v2/gaussdb-agent-runtime/manifests/v1")
    assert code == 200 and body == man and h["Docker-Content-Digest"] == man_d
    assert h["Content-Type"] == "application/vnd.oci.image.manifest.v1+json"
    code, h, body = _get(base + "/v2/gaussdb-agent-runtime/manifests/v1", method="HEAD")
    assert code == 200 and body == b"" and h["Content-Length"] == str(len(man))
    assert _get(base + "/v2/gaussdb-agent-runtime/manifests/" + man_d)[2] == man
    code, h, body = _get(base + "/v2/gaussdb-agent-runtime/blobs/" + layer)
    assert code == 200 and body == b"layer-bytes" * 100


@pytest.mark.parametrize("path", ["/v2/gaussdb-agent-runtime/manifests/v2", "/v2/other/manifests/v1",
                                  "/v2/x/blobs/sha256:" + "f" * 64, "/v2/x/blobs/sha256:../../index.json",
                                  "/v2/x/blobs/../../etc/passwd", "/etc/passwd"])
def test_unknown_or_malformed_references_are_404(layout, path):
    assert _get(layout[0] + path)[0] == 404
