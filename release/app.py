"""发版台的业务层:资源配置、计划预览、开始 / 回滚、扫描、集群大盘数据。HTTP 在 server.py。"""
from __future__ import annotations

import base64
import pathlib
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import package, plan as planmod, pool, steps, wldb
from .engine import Engine
from .kube import Kube, KubeError
from .scan import Scanner
from .store import Store

MASK = "●●●"
SCAN_MAX_AGE_S = 3600
# 资源配置字段:类型、可选值。未知字段拒收(防止拼错的键静默不生效)
FIELDS: Dict[str, Tuple[type, Optional[Tuple[str, ...]]]] = {
    "k8s.namespace": (str, None), "k8s.context": (str, None), "k8s.arch": (str, ("", "amd64", "arm64")),
    "registry.mode": (str, ("push", "preloaded")), "registry.prefix": (str, None), "registry.username": (str, None),
    "registry.plain_http": (bool, None), "registry.insecure": (bool, None), "registry.ca_file": (str, None),
    "model.base_url": (str, None), "model.id": (str, None), "model.provider": (str, None), "model.modalities": (str, None),
    "grmp.host": (str, None), "grmp.port": (str, None), "grmp.path_prefix": (str, None), "grmp.appkey": (str, None),
    "grmp.sign_mode": (str, ("", "full", "headers-only")), "grmp.instance_aliases": (str, None),
    "net.grmp": (dict, None), "net.model": (dict, None), "net.model_kb": (dict, None), "net.apiserver": (dict, None),
    "net.upstream": (dict, None),
    "pool.pods": (int, None), "pool.slots": (int, None), "gateway.runtime_mode": (str, ("", "ondemand", "pool")),
    "gateway.pull_policy": (str, ("", "IfNotPresent", "Always")), "sso.user_header": (str, None), "sso.roles_header": (str, None),
    "kb.clip_origins": (str, None), "whitelist.source": (str, ("csv", "db")), "whitelist.db.host": (str, None),
    "whitelist.db.port": (str, None), "whitelist.db.database": (str, None), "whitelist.db.user": (str, None),
    "smoke.user": (str, None), "smoke.instance": (str, None),
}
SENSITIVE_FIELDS = {"grmp.path_prefix"}
TOOL_SECRETS = {"registry_password", "whitelist_password"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, store: Store, releases: pathlib.Path, state: pathlib.Path, kubeconfig: str = "",
                 kubectl: str = "kubectl"):
        self.store, self.releases, self.state = store, releases, state
        self.kubeconfig, self.kubectl = kubeconfig, kubectl
        self.engine = Engine(store, steps.STEPS, services={"svc": self._services()})
        self._scan_lock = threading.Lock()
        self.scanning = False
        self._cluster: Tuple[float, dict] = (0.0, {})

    def reattach(self) -> List[int]:
        """启动时调用:停在关卡上的发布(状态 waiting)原来的执行线程随上一个进程没了,界面上点了也不会往下走。
        接回来:重新起线程等原来那个关卡(关卡本身持久化在状态库)。正在执行的那种由 store.mark_interrupted 标成暂停。"""
        out = []
        for r in self.store.runs(20):
            if r["status"] == "waiting" and not self.engine.busy():
                self.engine.resume(r["id"])
                out.append(r["id"])
        return out

    # -- 基础 -------------------------------------------------------------------------------------

    def kube(self) -> Kube:
        cfg = self.store.config()
        return Kube(namespace=cfg.get("k8s.namespace") or "gaussdb-agent", kubeconfig=self.kubeconfig,
                    context=cfg.get("k8s.context") or "", kubectl=self.kubectl)

    def _services(self) -> steps.Services:
        return steps.Services(store=self.store, kube=self.kube(), releases=self.releases, state=self.state, db_rows=wldb.rows)

    def packages(self) -> List[package.Package]:
        return package.discover(self.releases) if self.releases.is_dir() else []

    def arch(self) -> str:
        return self.store.config().get("k8s.arch") or self.store.kv_get("node_arch") or "amd64"

    # -- 资源配置 ----------------------------------------------------------------------------------

    def config_view(self) -> dict:
        cfg = dict(self.store.config())
        for k in SENSITIVE_FIELDS:
            if cfg.get(k):
                cfg[k] = MASK
        return {"config": cfg, "secrets": self.store.secret_marks(), "versions": self.store.config_versions(),
                "fields": sorted(FIELDS)}

    def save_config(self, incoming: Dict[str, Any], note: str = "") -> int:
        old = self.store.config()
        cfg: Dict[str, Any] = {}
        for k, v in incoming.items():
            if k not in FIELDS:
                raise ApiError(400, "不认识的配置项 %s" % k)
            typ, choices = FIELDS[k]
            if k in SENSITIVE_FIELDS and v == MASK:
                v = old.get(k, "")
            if typ is int and isinstance(v, str) and v.strip().isdigit():
                v = int(v)
            if v is not None and not isinstance(v, typ):
                raise ApiError(400, "%s 应当是 %s" % (k, typ.__name__))
            if choices and v not in choices:
                raise ApiError(400, "%s 只能是 %s" % (k, " / ".join(c or "(空)" for c in choices)))
            if typ is dict and v and not (isinstance(v.get("cidrs"), list) and all(isinstance(c, str) for c in v["cidrs"])):
                raise ApiError(400, "%s 要写成 {cidrs: [网段…], ports: [端口…]}" % k)
            cfg[k] = v
        changed = sorted(k for k in set(cfg) | set(old) if cfg.get(k) != old.get(k))
        vid = self.store.save_config(cfg, note or "资源配置")
        self.store.audit("修改资源配置", "配置版本 #%d" % vid,
                         "、".join(k + ("(值不记录)" if k in SENSITIVE_FIELDS else "") for k in changed) or "无变化")
        self.engine.services["svc"] = self._services()
        return vid

    def set_secret(self, name: str, value: str) -> dict:
        if name not in TOOL_SECRETS and name not in steps.CLUSTER_SECRETS:
            raise ApiError(400, "不认识的密钥 %s" % name)
        if not value or len(value) < 4:
            raise ApiError(400, "密钥太短")
        written = ""
        if name in steps.CLUSTER_SECRETS:
            sec, key, _req, _gen = steps.CLUSTER_SECRETS[name]
            kube = self.kube()
            cur = kube.get("secret", sec)
            if cur is not None:                     # 集群里已有:直接改集群,发版台只留指纹
                data = {k: base64.b64decode(v).decode("utf-8") for k, v in (cur.get("data") or {}).items()}
                data[key] = value
                kube.apply_secret(sec, data)
                written = sec
        fp = self.store.set_secret(name, value, keep=not written)
        where = ("已写入集群 Secret %s;" % written if written else
                 "发版台自己用,存在状态库;" if name in TOOL_SECRETS else "首次部署时写入集群;")
        self.store.audit("更换密钥", name, where + "值不记录")
        return {"fingerprint": fp, "written_to": written}

    # -- 扫描 -------------------------------------------------------------------------------------

    def start_scan(self, tag: str = "") -> None:
        with self._scan_lock:
            if self.scanning:
                raise ApiError(409, "扫描正在进行")
            self.scanning = True

        def run() -> None:
            try:
                Scanner(store=self.store, kube=self.kube(), releases=self.releases, target=tag).run()
            finally:
                self.scanning = False
        threading.Thread(target=run, name="scan", daemon=True).start()

    def scan_view(self) -> dict:
        last = self.store.last_scan()
        return {"running": self.scanning, "last": last,
                "fresh": bool(last and time.time() - last["at"] < SCAN_MAX_AGE_S)}

    # -- 计划与发布 --------------------------------------------------------------------------------

    def preview(self, tag: str) -> Tuple[planmod.Plan, package.Package]:
        pkgs = self.packages()
        target = next((p for p in pkgs if p.tag == tag), None)
        if target is None:
            raise ApiError(404, "发布包目录里没有 %s" % tag)
        cur = planmod.detect(self.kube())
        return planmod.build(cur, target, package.history(pkgs)), target

    def start(self, tag: str) -> int:
        sc = self.scan_view()
        if not sc["fresh"] or sc["last"]["status"] != "ok":
            raise ApiError(409, "先做资源扫描,全部可用(且在 1 小时内)才能开始发布")
        p, _target = self.preview(tag)
        if p.problems:
            raise ApiError(409, ";".join(p.problems))
        self.engine.services["svc"] = self._services()
        meta = dict(p.as_dict(), arch=self.arch())
        return self.engine.start(p.kind, p.from_v, p.to_v, p.steps, meta, None)

    def rollback(self, tag: str) -> int:
        p, _t = self.preview(tag)
        if p.kind != "rollback":
            raise ApiError(409, "%s 不是比当前更旧的版本" % tag)
        if p.problems:
            raise ApiError(409, ";".join(p.problems))
        self.engine.services["svc"] = self._services()
        return self.engine.start(p.kind, p.from_v, p.to_v, p.steps, dict(p.as_dict(), arch=self.arch()), None)

    def run_view(self, rid: int) -> dict:
        r = self.store.run(rid)
        if r is None:
            raise ApiError(404, "没有发布 #%d" % rid)
        r["shared"] = {k: v for k, v in self.store.kv_get("run:%d" % rid, {}).items() if k not in ("rendered", "pool_file")}
        r["meta"] = self.store.kv_get("run:%d:meta" % rid, {})
        return r

    def diff_text(self, rid: int) -> str:
        p = self.state / "runs" / str(rid) / "diff.txt"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    # -- 大盘 --------------------------------------------------------------------------------------

    def cluster_view(self) -> dict:
        at, cached = self._cluster
        if time.time() - at < 3:
            return cached
        kube, out = self.kube(), {"ok": True}
        try:
            cm = (kube.get("configmap", "gateway-config") or {}).get("data") or {}
            out["user_tag"] = cm.get("USER_IMAGE_TAG", "")
            out["gateway"] = [{"name": p["metadata"]["name"], "tag": planmod._image_tag(p["spec"]["containers"][0]["image"]),
                               "ready": all(c.get("ready") for c in (p.get("status") or {}).get("containerStatuses") or [])}
                              for p in kube.list("pods", "app=agent-gateway")]
            try:
                slots = pool.GatewayAdmin(kube).slots()
            except (pool.PoolError, KubeError, ValueError):
                slots = {}
            out["pool"] = []
            for p in sorted(kube.list("pods", "app=gaussdb-agent,role=pool"), key=lambda x: x["metadata"]["name"]):
                name = p["metadata"]["name"]
                tags = {c["name"]: planmod._image_tag(c["image"]) for c in p["spec"]["containers"]}
                out["pool"].append({"name": name, "steward": tags.get("steward", ""), "slots": [
                    {"slot": i, "tag": tags.get("slot-%d" % i, ""), "state": (slots.get((name, i)) or {}).get("state", "unknown"),
                     "busy": bool((slots.get((name, i)) or {}).get("busy"))}
                    for i in sorted(int(n.split("-")[1]) for n in tags if n.startswith("slot-"))]})
            out["kb_import"] = len(kube.list("deploy", "role=kb-import"))
            out["kbstd"] = [{"name": j["metadata"]["name"], "done": bool((j.get("status") or {}).get("succeeded"))}
                            for j in kube.list("jobs", "app=gaussdb-kbstd")]
        except KubeError as exc:
            out = {"ok": False, "error": str(exc)}
        self._cluster = (time.time(), out)
        return out

    def overview(self) -> dict:
        pk = [{"tag": p.tag, "version": p.version, "legacy": p.legacy, "ok": p.ok, "problems": p.problems,
               "arches": p.arches, "whitelist": ((p.release or {}).get("whitelist") or {}).get("count"),
               "notes": [n for h in (p.release or {}).get("history") or [] if h["version"] == p.version for n in h.get("notes") or []]}
              for p in self.packages()]
        return {"current": self.store.kv_get("current"), "packages": pk, "active": self.store.active_run(),
                "scan": self.scan_view(), "runs": self.store.runs(10)}
