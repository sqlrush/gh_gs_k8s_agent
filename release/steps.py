"""各步骤的实现:把 package / registry / render / whitelist / pool / smoke 串起来。每一步都要幂等(续跑会重跑整步)。

run 的 meta(引擎在开始时存下):kind、from_v、to_v、to_tag、pool_mode、required、special、whitelist_files、notes、arch。
"""
from __future__ import annotations

import base64
import pathlib
import secrets as _secrets
import sys
import tarfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import yaml

from . import oci, package, plan as planmod, pool, registry, render, smoke, whitelist, wldb
from .engine import Ctx, StepFailed
from .kube import Kube
from .store import Store

# 资源配置里的密钥名 → (Secret, 键, 必填, 缺了由发版台生成)
CLUSTER_SECRETS = {
    "model_api_key": ("model-api", "MODEL_API_KEY", True, False),
    "trust_secret": ("gateway-auth", "TRUST_SECRET", True, False),
    "admin_token": ("gateway-auth", "ADMIN_TOKEN", False, True),
    "grmp_sm2": ("grmp-sm2", "GRMP_SM2_PRIVATE_KEY", False, False),
    "sandbox_control": ("sandbox-control", "KEY", False, True),
}


@dataclass
class Services:
    store: Store
    kube: Kube
    releases: pathlib.Path
    state: pathlib.Path
    python: str = sys.executable
    sleep: Callable[[float], None] = time.sleep
    db_rows: Optional[Callable[[Dict[str, Any], str], List[tuple]]] = None    # 白名单只读账号:(配置, 密码) → 行
    swapper: Optional[Callable[..., pool.Swapper]] = None

    def cfg(self) -> Dict[str, Any]:
        return self.store.config()


def _svc(ctx: Ctx) -> Services:
    return ctx.services["svc"]


def _pkg(ctx: Ctx) -> package.Package:
    tag = ctx.plan["to_tag"]
    for p in package.discover(_svc(ctx).releases):
        if p.tag == tag:
            return p
    raise StepFailed("发布包目录里找不到 %s 的包" % tag)


def _run_dir(ctx: Ctx) -> pathlib.Path:
    d = _svc(ctx).state / "runs" / str(ctx.run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# -- 读包与校验 / 生成计划 ---------------------------------------------------------------------------

def step_pkg(ctx: Ctx) -> None:
    pkg = _pkg(ctx)
    ctx.sub(0, "done", pkg.root.name)
    ctx.sub(1, "running")
    problems = package.verify(pkg)
    if problems:
        raise StepFailed(";".join(problems))
    ctx.sub(1, "done", "全部一致")
    ctx.log("sha256:%d 个文件全部一致" % (len(pkg.files) + (0 if pkg.legacy else 1)), "g")
    ctx.sub(2, "done", "旧包(没有 release.yaml)" if pkg.legacy else pkg.release.get("version"))
    arch = ctx.plan.get("arch") or "amd64"
    if arch not in pkg.files:
        raise StepFailed("包里没有 %s 架构的镜像(节点是 %s)" % (arch, arch))
    try:
        digests = package.image_digests(pkg, arch)
    except oci.OciError as exc:
        raise StepFailed(str(exc))
    ctx.share(digests=digests)
    ctx.sub(3, "done", "%d 个镜像" % len(digests))
    ctx.detail("校验通过 · %s · %d 个镜像" % (arch, len(digests)))


def step_plan(ctx: Ctx) -> None:
    cur = planmod.detect(_svc(ctx).kube)
    ctx.sub(0, "done", cur.tag or "未部署")
    want = ctx.plan.get("from_v") or ""
    if ctx.plan["kind"] in ("upgrade", "rollback") and cur.version != want:
        raise StepFailed("集群现在是 %s,生成计划时是 %s:集群被别人改过,请回到发布页重新生成计划" % (cur.version or "未部署", want))
    ctx.sub(1, "done")
    for n in ctx.plan.get("notes") or []:
        ctx.log("提醒:" + n, "w")
    ctx.sub(2, "done", "%s · 池子%s" % (ctx.plan["kind"], {"slot": "按槽位在线", "pod": "按 Pod", "fresh": "新建"}[ctx.plan["pool_mode"]]))
    ctx.detail("%s → %s" % (want or "未部署", ctx.plan["to_v"]))


# -- 镜像入库 ----------------------------------------------------------------------------------------

def registry_client(store: Store, cfg: Dict[str, Any]) -> registry.Client:
    return registry.Client(registry.Endpoint(prefix=str(cfg.get("registry.prefix") or "").rstrip("/"),
                                             username=str(cfg.get("registry.username") or ""),
                                             password=store.secret("registry_password"),
                                             plain_http=bool(cfg.get("registry.plain_http")),
                                             insecure=bool(cfg.get("registry.insecure")),
                                             ca_file=str(cfg.get("registry.ca_file") or "")))


def step_images(ctx: Ctx) -> None:
    svc, cfg = _svc(ctx), _svc(ctx).cfg()
    if cfg.get("registry.mode") == "preloaded":
        for k in range(3):
            ctx.sub(k, "done", "已预置")
        ctx.log("资源配置为「镜像已预置在节点上」:跳过推送(只用于测试环境)", "w")
        ctx.detail("已预置,跳过")
        return
    pkg, arch = _pkg(ctx), ctx.plan.get("arch") or "amd64"
    client = registry_client(svc.store, cfg)
    client.log = lambda m: ctx.log(m)
    ctx.sub(0, "running")
    try:
        client.ping()
    except (registry.RegistryError, OSError) as exc:
        raise StepFailed("内网仓库不可用:%s" % exc)
    ctx.sub(0, "done", client.ep.prefix)
    imgs = oci.read_images(str(pkg.files[arch]), arch)
    ctx.sub(1, "running")
    seen: Dict[str, str] = {}

    def progress(repo: str, done: int, total: int) -> None:
        note = "%s %d/%d" % (repo.replace("gaussdb-agent-", ""), done, total)
        if seen.get(repo) != note:
            seen[repo] = note
            ctx.sub(1, "running", note)
    try:
        results = registry.push_images(client, str(pkg.files[arch]), imgs, lambda i: ctx.plan["to_tag"], progress)
    except (registry.RegistryError, oci.OciError, OSError) as exc:
        raise StepFailed("推送失败:%s" % exc)
    ctx.sub(1, "done", "上传 %.1f MB" % (sum(r.bytes_sent for r in results) / 1048576))
    for r in results:
        ctx.log("%s:%s 上传 %d、挂载 %d、已有 %d 个 blob,digest %s" % (r.repo, r.tag, r.uploaded, r.mounted, r.existing, r.digest[:19]), "g")
    ctx.share(pushed=[{"repo": r.repo, "tag": r.tag, "digest": r.digest, "uploaded": r.uploaded, "mounted": r.mounted,
                       "existing": r.existing, "mb": round(r.bytes_sent / 1048576, 1)} for r in results])
    ctx.sub(2, "done", "%d 个镜像 digest 一致" % len(results))
    ctx.detail("%d/%d 已入库" % (len(results), len(imgs)))


# -- 配置迁移与渲染 / 变更预览 ---------------------------------------------------------------------------

def _extract_k8s(pkg: package.Package, dest: pathlib.Path) -> pathlib.Path:
    """只解 k8s/ 下的文件;拒绝绝对路径与 ..(包是外来文件)。"""
    root = dest / "k8s"
    if root.is_dir():
        return root
    with tarfile.open(pkg.files["k8s"], "r:gz") as tf:
        for m in tf:
            name = m.name.lstrip("./")
            if not name.startswith("k8s/") or ".." in pathlib.PurePosixPath(name).parts or m.name.startswith("/"):
                continue
            if m.isdir():
                (dest / name).mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                (dest / name).write_bytes(tf.extractfile(m).read())
    if not (root / "base" / "kustomization.yaml").is_file():
        raise StepFailed("k8s 清单包里没有 k8s/base/kustomization.yaml")
    return root


def _cluster_state(kube: Kube) -> tuple:
    cms = {}
    for n in ("agent-config", "gateway-config", "agent-roles"):
        d = kube.get("configmap", n)
        if d is not None:
            cms[n] = {k: str(v) for k, v in (d.get("data") or {}).items()}
    nps = {d["metadata"]["name"]: d["spec"] for d in kube.list("networkpolicy")}
    existing = []
    if kube.get("namespace", kube.namespace, namespaced=False):
        existing.append("Namespace/" + kube.namespace)
    for kind, name, ref in (("pvc", "nas", "PersistentVolumeClaim/nas"), ("deploy", "gateway", "Deployment/gateway")):
        if kube.get(kind, name):
            existing.append(ref)
    return cms, nps, existing


def step_config(ctx: Ctx) -> None:
    svc = _svc(ctx)
    cfg, kube = svc.cfg(), svc.kube
    pkg = _pkg(ctx)
    k8s_root = _extract_k8s(pkg, _run_dir(ctx))
    ctx.sub(0, "running")
    cms, nps, existing = _cluster_state(kube)
    ctx.sub(0, "done", "%d 个 ConfigMap、%d 条网络策略" % (len(cms), len(nps)))
    pins = pinned_digests(ctx, cfg)
    out = render.render(k8s_root, cfg, ctx.plan["to_tag"], cms, nps, existing, ctx.plan.get("required") or [], pins)
    ctx.sub(1, "done", "%d 项变化" % len(out.changes))
    for c in out.changes:
        s = c.show()
        ctx.log("%s %s:%s → %s(%s)" % (s["obj"], s["key"], s["old"] if s["old"] is not None else "(无)", s["new"], s["why"]))
    if out.problems:
        ctx.sub(2, "failed", "%d 项" % len(out.problems))
        raise StepFailed(";".join(out.problems))
    ctx.sub(2, "done", "齐全")
    run_dir = _run_dir(ctx)
    (run_dir / "rendered.yaml").write_text(out.yaml(), encoding="utf-8")
    pool_file = ""
    if ctx.plan["kind"] == "deploy":
        image = render_runtime_image(cfg, ctx.plan["to_tag"], pins.get(RUNTIME, ""))
        pool_docs = render.render_pool(k8s_root, cfg, image, kube.namespace, python=svc.python)
        (run_dir / "pool.yaml").write_text(yaml.safe_dump_all(pool_docs, allow_unicode=True, sort_keys=False), encoding="utf-8")
        pool_file = str(run_dir / "pool.yaml")
    ctx.sub(3, "done", "%d 个对象" % len(out.docs))
    ctx.sub(4, "running")
    diff = kube.diff(out.yaml()) if "Namespace/" + kube.namespace in existing else "(命名空间还不存在:全部新建)"
    (run_dir / "diff.txt").write_text(_mask_cm_values(clean_diff(diff)), encoding="utf-8")
    vid = svc.store.save_config(cfg, "发布 #%d 渲染(%s)" % (ctx.run_id, ctx.plan["to_v"]))
    svc.store.update_run(ctx.run_id, config_version=vid)
    ctx.share(rendered=str(run_dir / "rendered.yaml"), pool_file=pool_file, changes=[c.show() for c in out.changes],
              skipped=out.skipped, objects=[render.ref(d) for d in out.docs], diff_lines=clean_diff(diff).count("\n"))
    ctx.sub(4, "done", "%d 行差异" % clean_diff(diff).count("\n"))
    ctx.detail("配置版本 #%d · %d 个对象 · %d 项配置变化" % (vid, len(out.docs), len(out.changes)))


RUNTIME = "gaussdb-agent-runtime"


def render_runtime_image(cfg: Dict[str, Any], tag: str, digest: str = "") -> str:
    return render.image_ref(str(cfg.get("registry.prefix") or "").rstrip("/"), RUNTIME, tag, digest)


def pinned_digests(ctx: Ctx, cfg: Dict[str, Any]) -> Dict[str, str]:
    """推进内网仓库的镜像按 digest 钉住(读包那步算好的)。预载模式(镜像直接载进节点)不钉:那样载进去的镜像
    没有仓库 digest,按 digest 找不到。网关自己建的用户容器只认 USER_IMAGE_TAG,不在这里。"""
    if (cfg.get("registry.mode") or "push") != "push":
        return {}
    return dict(ctx.shared().get("digests") or {})


NOISE = ("kubectl.kubernetes.io/last-applied-configuration", "generation:", "resourceVersion:", "creationTimestamp:", "uid:")


def clean_diff(diff: str) -> str:
    """kubectl diff 原样给人看太杂:临时文件路径、上次 apply 的整段 JSON、generation 计数。只留对象名与真正变化的行。"""
    out: List[str] = []
    for line in diff.splitlines():
        if line.startswith("diff -u"):
            out.append("== " + line.rsplit("/", 1)[-1])
        elif line.startswith(("---", "+++", "@@")) or any(n in line for n in NOISE) or line.strip().startswith('{"apiVersion"'):
            continue
        else:
            out.append(line)
    return "\n".join(out)


def _mask_cm_values(diff: str) -> str:
    """变更预览给人看:ConfigMap 里敏感键的值换成「已设置」。"""
    out = []
    for line in diff.splitlines():
        for key in render.SENSITIVE:
            if line.lstrip("+- ").startswith(key + ":"):
                line = line[: line.index(key) + len(key)] + ": (已设置,不显示)"
        out.append(line)
    return "\n".join(out)


def step_review(ctx: Ctx) -> None:
    sh = ctx.shared()
    ctx.sub(0, "done", "%d 行差异" % sh.get("diff_lines", 0))
    ctx.sub(1, "waiting")
    ctx.gate("review", {"objects": len(sh.get("objects") or []), "changes": len(sh.get("changes") or []),
                        "diff_lines": sh.get("diff_lines", 0)})
    ctx.sub(1, "done")
    ctx.sub(2, "done", "已确认")
    ctx.detail("已确认")


# -- 白名单核对 ------------------------------------------------------------------------------------------

def registered_scripts(svc: Services, cfg: Dict[str, Any]) -> List[whitelist.Registered]:
    src = cfg.get("whitelist.source") or "csv"
    if src == "csv":
        text = svc.store.kv_get("whitelist.export", "")
        if not text:
            raise StepFailed("还没上传白名单导出文件:在白名单核对页上传按界面给出的 SELECT 导出的 CSV")
        return whitelist.from_csv(text)
    if svc.db_rows is None:
        raise StepFailed("发版台镜像里没有数据库驱动,改用「导出文件」方式")
    try:
        return whitelist.from_rows(svc.db_rows(cfg, svc.store.secret("whitelist_password")))
    except wldb.DbError as exc:
        raise StepFailed(str(exc))


def step_whitelist(ctx: Ctx) -> None:
    svc = _svc(ctx)
    pkg = _pkg(ctx)
    expected = whitelist.from_json((pkg.release or {}).get("whitelist", {}).get("scripts") or [])
    if not expected:
        raise StepFailed("目标包的 release.yaml 里没有白名单清单")
    ctx.sub(0, "done", "%d 条" % len(expected))
    while True:
        ctx.sub(1, "running")
        rep = whitelist.compare(expected, registered_scripts(svc, svc.cfg()))
        ctx.sub(1, "done", "已读取")
        counts = rep.counts()
        ctx.sub(2, "done", "%d 条一致" % counts[whitelist.OK])
        ctx.share(whitelist={"expected": rep.expected, "counts": counts, "extra": rep.extra,
                             "problems": [{"name": f.name, "status": f.status, "label": whitelist.LABELS[f.status],
                                           "detail": f.detail} for f in rep.problems]})
        if rep.passed:
            ctx.sub(3, "done", "%d/%d" % (rep.expected, rep.expected))
            ctx.log("核对通过:%d 条全部已登记且一致" % rep.expected, "g")
            ctx.detail("%d/%d 核对通过" % (rep.expected, rep.expected))
            return
        msg = "、".join("%s %d 条" % (whitelist.LABELS[k], v) for k, v in counts.items() if v and k != whitelist.OK)
        ctx.log("核对未通过:%s" % msg, "w")
        ctx.sub(3, "waiting", msg)
        ctx.detail("未通过:" + msg)
        ctx.gate("whitelist", {"counts": counts, "files": ctx.plan.get("whitelist_files") or []})
        ctx.log("运维确认已补登记,重新核对")


# -- 网关 / 基础资源 -------------------------------------------------------------------------------------

def _docs(path: str) -> List[dict]:
    return [d for d in yaml.safe_load_all(pathlib.Path(path).read_text(encoding="utf-8")) if d]


def write_cluster_secrets(svc: Services, ctx: Ctx) -> None:
    """首次部署:把资源配置里临时存着的密钥写进集群,写完发版台只留指纹;该生成的随机生成(不显示给任何人)。"""
    kube, store = svc.kube, svc.store
    groups: Dict[str, Dict[str, str]] = {}
    for name, (sec, key, required, generate) in CLUSTER_SECRETS.items():
        val = store.secret(name)
        if not val and generate:
            val = _secrets.token_hex(24)
        if not val:
            if required:
                raise StepFailed("资源配置里还没设置「%s」" % name)
            continue
        groups.setdefault(sec, {})[key] = val
        store.set_secret(name, val, keep=False)
    for sec, data in groups.items():
        kube.apply_secret(sec, data)
        ctx.log("已写入 Secret %s(%s;值不显示)" % (sec, "、".join(sorted(data))))


def step_gateway(ctx: Ctx) -> None:
    svc = _svc(ctx)
    kube = svc.kube
    docs = [d for d in _docs(ctx.shared()["rendered"]) if d.get("kind") != "Job"]
    if ctx.plan["kind"] == "deploy":
        if kube.get("namespace", kube.namespace, namespaced=False) is None:
            ns = [d for d in docs if d["kind"] == "Namespace"]
            kube.apply(yaml.safe_dump_all(ns, allow_unicode=True))
        write_cluster_secrets(svc, ctx)
    ctx.sub(0, "running")
    kube.apply(yaml.safe_dump_all([d for d in docs if d["kind"] != "Namespace"], allow_unicode=True, sort_keys=False))
    ctx.sub(0, "done", "%d 个对象" % len(docs))
    ctx.log("已应用:%s" % "、".join(render.ref(d) for d in docs))
    deploys = [d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"]
    for i, name in enumerate(deploys):
        ctx.sub(1, "running", name)
        try:
            kube.rollout_status("deploy/" + name, 300)
        except Exception as exc:                    # noqa: BLE001
            raise StepFailed("deploy/%s 300 秒内没有滚动完成:%s" % (name, exc))
        ctx.log("deploy/%s 已就绪" % name, "g")
    ctx.sub(1, "done", "、".join(deploys))
    ctx.sub(2, "done")
    ctx.detail("%d 个 Deployment 就绪" % len(deploys))


# -- 池子 ----------------------------------------------------------------------------------------------

def step_pool(ctx: Ctx) -> None:
    svc = _svc(ctx)
    kube, cfg = svc.kube, svc.cfg()
    image = render_runtime_image(cfg, ctx.plan["to_tag"], pinned_digests(ctx, cfg).get(RUNTIME, ""))
    sts = kube.get("statefulset", "agent-pool")
    if ctx.plan["kind"] == "deploy":
        if not ctx.shared().get("pool_file"):
            raise StepFailed("没有渲染出池子清单")
        kube.apply(pathlib.Path(ctx.shared()["pool_file"]).read_text(encoding="utf-8"))
        ctx.sub(0, "done")
        ctx.sub(1, "done")
        adm = pool.GatewayAdmin(kube)
        end = time.time() + 600
        while True:
            ctx.check()
            try:
                slots = adm.slots()
            except pool.PoolError:
                slots = {}
            if slots and all(s.get("state") == "free" for s in slots.values()):
                break
            if time.time() > end:
                raise StepFailed("池子 600 秒内没有全部空闲")
            svc.sleep(5)
        ctx.sub(2, "done", "%d 个槽位空闲" % len(slots))
        ctx.detail("%d 个槽位就绪" % len(slots))
        return
    if sts is None:
        for k in range(3):
            ctx.sub(k, "done", "按需模式")
        ctx.log("没有固定池化(按需模式):用户下次登录时网关按新的 USER_IMAGE_TAG 建容器")
        ctx.detail("按需模式,无需替换")
        return
    kube.patch("statefulset", "agent-pool", render.pool_image_patch(sts, image))
    ctx.sub(0, "done", "模板已换 → %s" % ctx.plan["to_tag"])
    pods = sorted(p["metadata"]["name"] for p in kube.list("pods", "app=gaussdb-agent,role=pool"))
    matrix: Dict[str, Dict[str, Any]] = {}

    def progress(pod_name: str, slot: int, st: str, ver: str) -> None:
        matrix.setdefault(pod_name, {})["steward" if slot < 0 else str(slot)] = {"st": st, "v": ver}
        ctx.share(pool=matrix)
    make = svc.swapper or (lambda **kw: pool.Swapper(**kw))
    sw = make(kube=kube, admin=pool.GatewayAdmin(kube), image=image, log=lambda lv, m: ctx.log(m, lv),
              progress=progress, gate=lambda kind, payload: ctx.gate(kind, payload))
    for i, name in enumerate(pods):
        ctx.check()
        ctx.sub(1, "running", "%s(%d/%d)" % (name, i + 1, len(pods)))
        try:
            if ctx.plan["pool_mode"] == "slot":
                sw.swap_pod_online(name)
            else:
                sw.replace_pod(name)
        except pool.PoolError as exc:
            raise StepFailed(str(exc))
        ctx.detail("%d/%d 个 Pod 已换" % (i + 1, len(pods)))
    ctx.sub(1, "done", "%d 个 Pod" % len(pods))
    ctx.sub(2, "done")
    if sw.aborted:
        ctx.log("有 %d 个槽位转保留时中断了正在执行的请求:%s" % (len(sw.aborted), "、".join(sw.aborted)), "w")


# -- 导入环境 / 知识库标准包 / 冒烟 / 完成 ----------------------------------------------------------------

def step_kbimport(ctx: Ctx) -> None:
    _svc(ctx).kube.delete("deploy,svc", selector="role=kb-import")
    ctx.sub(0, "done", "管理员下次进入导入环境时按新模板建;NAS 数据不动")
    ctx.log("已删除旧的导入容器(role=kb-import)")


def step_kbstd(ctx: Ctx) -> None:
    kube = _svc(ctx).kube
    jobs = [d for d in _docs(ctx.shared()["rendered"]) if d.get("kind") == "Job"]
    if not jobs:
        raise StepFailed("目标版本的清单里没有知识库标准包安装 Job")
    name = jobs[0]["metadata"]["name"]
    kube.apply(yaml.safe_dump_all(jobs, allow_unicode=True, sort_keys=False))
    ctx.sub(0, "done", name)
    ctx.sub(1, "running")
    res = kube.run(["wait", "--for=condition=complete", "job/" + name, "--timeout=900s"], timeout=960, check=False)
    if res.code != 0:
        raise StepFailed("Job %s 没有完成:%s(kubectl -n %s logs job/%s 看原因)" % (name, res.err.strip()[:200], kube.namespace, name))
    ctx.sub(1, "done")
    out = kube.logs("job/" + name, "install", 50)
    verdict = "[installed]" if "[installed]" in out else "[skipped]" if "[skipped]" in out else ""
    for line in out.splitlines()[-5:]:
        ctx.log(line)
    if not verdict:
        raise StepFailed("Job %s 完成了,但日志里没有 [installed] / [skipped]" % name)
    for j in kube.list("jobs", "app=gaussdb-kbstd"):
        old = j["metadata"]["name"]
        if old != name:
            kube.delete("job", old)
            ctx.log("删除旧的 Job %s" % old)
    ctx.sub(2, "done", "同版本跳过" if verdict == "[skipped]" else "已安装")
    ctx.detail(("同版本跳过" if verdict == "[skipped]" else "已安装") + " · Completed")


def step_smoke(ctx: Ctx) -> None:
    svc = _svc(ctx)
    kube, cfg = svc.kube, svc.cfg()
    sec = kube.get("secret", "gateway-auth") or {}
    trust = base64.b64decode(((sec.get("data") or {}).get("TRUST_SECRET") or "")).decode("utf-8")
    if not trust:
        raise StepFailed("集群里没有 gateway-auth 的 TRUST_SECRET,冒烟登录不了")
    svc.store.remember_secret(trust)
    user = str(cfg.get("smoke.user") or "")
    if not user:
        raise StepFailed("资源配置里没填冒烟工号")
    gwcfg = (kube.get("configmap", "gateway-config") or {}).get("data") or {}
    names = ["模拟登录", "真实对话", "诊断报告", "知识库检索"]
    with kube.port_forward("svc/gateway", 80) as port:
        s = smoke.Smoke(host="127.0.0.1", port=port, user=user, trust=trust,
                        user_header=gwcfg.get("USER_HEADER") or "X-Agent-User", instance=str(cfg.get("smoke.instance") or ""),
                        log=lambda m: ctx.log(m, "g" if m.startswith("✓") else "e"))
        ok = s.run(ctx.plan["to_v"])
    by = {c.name: c for c in s.results}
    for k, n in enumerate(names):
        c = by.get(n if n != "诊断报告" else "只读诊断报告")
        ctx.sub(k, "done" if c and c.ok else "failed" if c else "skipped", (c.detail[:60] if c else "未配置冒烟实例"))
    ctx.share(smoke=[{"name": c.name, "ok": c.ok, "detail": c.detail, "seconds": c.seconds} for c in s.results])
    if not ok:
        bad = next(c for c in s.results if not c.ok)
        raise StepFailed("冒烟验证未通过:%s —— %s" % (bad.name, bad.detail))
    ctx.detail("%d 项全部通过" % len(s.results))


def step_done(ctx: Ctx) -> None:
    store = _svc(ctx).store
    run = store.run(ctx.run_id)
    store.kv_set("current", {"version": ctx.plan["to_v"], "tag": ctx.plan["to_tag"], "config_version": run["config_version"],
                             "run": ctx.run_id, "at": time.time()})
    ctx.sub(0, "done", ctx.plan["to_v"])
    ctx.sub(1, "done")
    ctx.log("当前版本记为 %s(配置版本 #%s)" % (ctx.plan["to_v"], run["config_version"]), "g")
    ctx.detail("发布报告已生成")


STEPS = {"pkg": step_pkg, "plan": step_plan, "images": step_images, "config": step_config, "review": step_review,
         "whitelist": step_whitelist, "gateway": step_gateway, "pool": step_pool, "kbimport": step_kbimport,
         "kbstd": step_kbstd, "smoke": step_smoke, "done": step_done}
