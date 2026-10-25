#!/usr/bin/env python3
"""网关「按工号建 Pod」那一步的参考实现(spec §8),也是 Mac 集群验证的工具。

  provision.py u1001 --auto-role            # 读角色表决定给不给 kb-import
  provision.py u2001 --kb-admin             # 强制加 kb-import
  provision.py u1001 --delete               # 回收 Deployment/Service/Secret(PVC 与 NAS 目录不动)

令牌只在内存里经 stdin 交给 kubectl,不落盘、不进日志。
"""
from __future__ import annotations

import argparse
import pathlib
import secrets
import string
import subprocess
import sys
import time
from typing import List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "k8s" / "templates"
NS = "gaussdb-agent"


def is_kb_admin(roles_text: str, user_id: str) -> bool:
    """与网关 gateway/pods.py:parse_kb_admins 同一口径:每行一个工号,`#` 后是注释,大小写不敏感。"""
    ids = {ln.split("#", 1)[0].strip().lower() for ln in roles_text.splitlines()}
    return user_id.strip().lower() in ids - {""}


def plan(user_id: str, roles_text: str, auto_role: bool, kb_admin_flag: bool) -> List[str]:
    out = ["runtime"]
    if kb_admin_flag or (auto_role and is_kb_admin(roles_text, user_id)):
        out.append("kb-import")
    return out


def render(template_text: str, **vars_: str) -> str:
    return string.Template(template_text).substitute(vars_)     # 缺占位符 → KeyError,宁可炸也不要渲染出空值


def _kubectl(ctx: str, *args: str, stdin: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "--context", ctx, "-n", NS, *args], input=stdin, text=True,
                          capture_output=True, check=check)


def read_token(text: str) -> Optional[str]:
    """从 env 文件文本里取 GRMP_AUTH_TOKEN(允许 `export ` 前缀与引号);没有 → None。"""
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("export "):
            ln = ln[len("export "):].strip()
        if ln.startswith("GRMP_AUTH_TOKEN="):
            return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _read_token(env_file: pathlib.Path) -> str:
    token = read_token(env_file.read_text(encoding="utf-8"))
    if not token:
        raise SystemExit("令牌文件里没有 GRMP_AUTH_TOKEN=:%s" % env_file)
    return token


def _apply_secret(ctx: str, name: str, key: str, value: str) -> None:
    doc = ("apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\ntype: Opaque\nstringData:\n  %s: %s\n"
           % (name, NS, key, value))
    _kubectl(ctx, "apply", "-f", "-", stdin=doc)


def _roles(ctx: str) -> str:
    r = _kubectl(ctx, "get", "configmap", "agent-roles", "-o", "jsonpath={.data.kb-admins}", check=False)
    return r.stdout if r.returncode == 0 else ""


def provision(user_id: str, ctx: str, tag: str, pull_policy: str, kinds: List[str],
              token_file: Optional[pathlib.Path], wait_s: int) -> None:
    if "runtime" in kinds:
        if token_file is None or not token_file.is_file():
            raise SystemExit("runtime 需要本人 GRMP 令牌:--token-env-file 指向含 GRMP_AUTH_TOKEN= 的文件")
        _apply_secret(ctx, "grmp-token-%s" % user_id, "GRMP_AUTH_TOKEN", _read_token(token_file))
    _apply_secret(ctx, "pod-auth-%s" % user_id, "OPENCODE_SERVER_PASSWORD", secrets.token_hex(24))
    for kind in kinds:
        text = render((TEMPLATES / f"{kind}.yaml").read_text(encoding="utf-8"), USER_ID=user_id,
                      IMAGE="gaussdb-agent-%s:%s" % (kind, tag), IMAGE_PULL_POLICY=pull_policy,
                      SUBPATH_ROOT="admins" if kind == "kb-import" else "users")
        _kubectl(ctx, "apply", "-f", "-", stdin=text)
        print("已创建 %s-%s" % (kind, user_id))
    for kind in kinds:
        name = "%s-%s" % (kind, user_id)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            r = _kubectl(ctx, "get", "deploy", name, "-o", "jsonpath={.status.availableReplicas}", check=False)
            if r.stdout.strip() == "1":
                print("%s 就绪" % name)
                break
            time.sleep(3)
        else:
            raise SystemExit("%s %d 秒内未就绪:kubectl --context %s -n %s describe deploy %s"
                             % (name, wait_s, ctx, NS, name))


def deprovision(user_id: str, ctx: str) -> None:
    for kind in ("runtime", "kb-import"):
        _kubectl(ctx, "delete", "deploy,svc", "%s-%s" % (kind, user_id), "--ignore-not-found", "--wait=true", check=False)
    for s in ("grmp-token-%s" % user_id, "pod-auth-%s" % user_id):
        _kubectl(ctx, "delete", "secret", s, "--ignore-not-found", check=False)
    print("已回收 %s 的 Pod/Service/Secret(NAS 目录保留)" % user_id)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("user_id")
    ap.add_argument("--context", default="orbstack")
    ap.add_argument("--image-tag", default="dev")
    ap.add_argument("--pull-policy", default="IfNotPresent")
    ap.add_argument("--kb-admin", action="store_true")
    ap.add_argument("--auto-role", action="store_true", help="读 ConfigMap agent-roles 决定是否给 kb-import")
    ap.add_argument("--token-env-file", default=str(pathlib.Path.home() / ".gdaa" / "grmp.env"))
    ap.add_argument("--wait-s", type=int, default=120)
    ap.add_argument("--delete", action="store_true")
    a = ap.parse_args(argv)
    if a.delete:
        deprovision(a.user_id, a.context)
        return 0
    kinds = plan(a.user_id, _roles(a.context) if a.auto_role else "", a.auto_role, a.kb_admin)
    provision(a.user_id, a.context, a.image_tag, a.pull_policy, kinds,
              pathlib.Path(a.token_env_file).expanduser(), a.wait_s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
