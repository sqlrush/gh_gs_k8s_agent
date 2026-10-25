#!/usr/bin/env python3
"""固定池化管家控制接口的命令行(测试与排障用,2026-10-23)。请求按 poolauth 签名、应答验签。

    export POOL_CONTROL_KEY=…            # Secret sandbox-control 的 KEY;只经环境变量,不上命令行
    kubectl -n gaussdb-agent port-forward pod/agent-pool-0 14098:4098 &
    pool-cli.py --url http://127.0.0.1:14098 --pod agent-pool-0 status
    pool-cli.py … bind --slot 0 --user u9501 --expect 0 --container-id <id> [--grmp-env GRMP_TOKEN]
    pool-cli.py … retain --slot 0 --user u9501
    pool-cli.py … resume --slot 0 --user u9501 --expect 0 --container-id <id>
    pool-cli.py … evict --slot 0 --container-id <id>
    pool-cli.py … verify --slot 0 --container-id <新 id>
    pool-cli.py … disable --slot 0 --reason ops

GRMP 令牌从 --grmp-env 指定的环境变量读,加密后放进请求体。应答原样输出 JSON(bind / resume 的应答含口令,
默认把 password / token 打成 ***;要拿去连 opencode 时加 --show-secrets)。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "docker"))

import poolauth  # noqa: E402

_SECRET_KEYS = ("password", "token")


def _mask(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k in _SECRET_KEYS and isinstance(v, str) and v else _mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask(x) for x in obj]
    return obj


def call(url: str, pod: str, key: bytes, method: str, path: str, body=None, timeout: float = 120):
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    hdrs = poolauth.sign_request(key, method, path, pod, raw)
    req = urllib.request.Request(url.rstrip("/") + path, data=raw if method == "POST" else None, method=method)
    for k, v in dict(hdrs, **{"Content-Type": "application/json"}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, data, sig = r.status, r.read(), r.headers.get(poolauth.HDR_RESP_SIG, "")
    except urllib.error.HTTPError as exc:
        code, data, sig = exc.code, exc.read(), exc.headers.get(poolauth.HDR_RESP_SIG, "")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit("连不上管家 %s:%s(port-forward 断了?)" % (url, getattr(exc, "reason", exc)))
    if code != 401 and not poolauth.check_response(key, pod, hdrs[poolauth.HDR_NONCE], code, data, sig):
        raise SystemExit("应答没有管家的签名(连到的不是管家?)")
    return code, json.loads(data or b"null")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", required=True)
    p.add_argument("--pod", required=True, help="目标 Pod 名(签进请求)")
    p.add_argument("--show-secrets", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("bind", "resume", "retain", "evict", "verify", "disable"):
        s = sub.add_parser(name)
        s.add_argument("--slot", type=int, required=True)
        if name in ("bind", "resume", "retain"):
            s.add_argument("--user", required=True)
        if name in ("bind", "resume"):
            s.add_argument("--expect", type=int, required=True, help="以为的在用人数(Pod 级 CAS)")
            s.add_argument("--grmp-env", default="", help="从这个环境变量读 GRMP 令牌")
        if name in ("bind", "resume", "evict", "verify"):
            s.add_argument("--container-id", default="")
        if name == "disable":
            s.add_argument("--reason", default="ops")
    a = p.parse_args(argv)
    key = os.environ.get("POOL_CONTROL_KEY", "").encode("utf-8")
    if len(key) < 16:
        raise SystemExit("环境变量 POOL_CONTROL_KEY 没设或太短")
    if a.cmd == "status":
        code, out = call(a.url, a.pod, key, "GET", "/pool/status")
    else:
        body = {"slot": a.slot}
        if hasattr(a, "user"):
            body["user_id"] = a.user
        if hasattr(a, "expect"):
            body["expect_in_use"] = a.expect
            if a.grmp_env:
                tok = os.environ.get(a.grmp_env, "")
                if not tok:
                    raise SystemExit("环境变量 %s 是空的" % a.grmp_env)
                body["grmp_token"] = poolauth.seal(key, tok)
        if getattr(a, "container_id", ""):
            body["container_id"] = a.container_id
        if hasattr(a, "reason"):
            body["reason"] = a.reason
        code, out = call(a.url, a.pod, key, "POST", "/pool/" + a.cmd, body)
    print(json.dumps({"http": code, "body": out if a.show_secrets else _mask(out)}, ensure_ascii=False))
    return 0 if code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
