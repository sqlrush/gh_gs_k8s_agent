"""Fuyao(E2B 沙箱)运行模式的两步:「模板核对」(网关滚动之前)与「沙箱换版」(网关滚动之后)。
设计:docs/specs/2026-11-17-fuyao-sandbox-design.md §6.2(换版)、§8(模板)。

- 模板名与 scripts/build-e2b-template.py 的 template_name 同一条规则。发版台镜像里只有 release/,运行时拿不到
  scripts/,所以规则在这里另写一份,release/tests 逐个标签比对两边,改了一边测试就会失败;
- 模板在平台的模板构建服务所在主机上构建(构建服务只从本机 127.0.0.1 的 HTTP 仓库拉镜像,跳板机做不了)。
  发版台只核对「目标版本的模板已经能建沙箱」:各建一台探测沙箱,进入 Running 就删掉;
- OpenSandbox 与网关的 /admin/fuyao 都只在集群里面通:经 kubectl exec 进网关容器调用(同 pool.GatewayAdmin),
  OpenSandbox 的 API Key、网关的运维密钥只在网关容器的环境变量里,不出容器;
- 探测沙箱的 metadata 只带探测标记(gsdb-probe),不带 gsdb-app —— 网关的清扫器与 /admin/fuyao 都看不到它们。
  每次探测完都按标记列举一遍,剩下的(包括上次中断留下的)全部删掉;删不掉就让这一步失败,续跑时再清;
- 换版不需要发版台删沙箱:旧版沙箱由网关在闲置时收尾删除、下次按新模板建,在用的不打断(outdated 是剩余台数)。
  唯一的例外是冒烟工号自己的旧版沙箱:冒烟前交给网关收尾删除,否则冒烟跑在旧模板上,验不到新版本。
"""
from __future__ import annotations

import pathlib
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

import yaml

from . import pool
from .engine import Ctx, StepFailed
from .kube import KubeError
from .render import FUYAO_TEMPLATE_KEYS

KINDS = ("runtime", "kb-import")                    # 与网关 gateway/pods.py 的 KIND_RUNTIME / KIND_KB_IMPORT 一致
KIND_NAMES = {"runtime": "诊断", "kb-import": "导入"}
TEMPLATE_KEYS = {k: key for k, (_cm, key) in FUYAO_TEMPLATE_KEYS.items()}       # gateway-config 的键
GROUP_KEYS = {"runtime": "FUYAO_RUNTIME_GROUP", "kb-import": "FUYAO_IMPORT_GROUP"}
PROBE_MARK = ("gsdb-probe", "release")              # 探测沙箱的 metadata;只有发版台建这种沙箱
BUILD_SCRIPT = "scripts/build-e2b-template.py"
PROBE_WAIT_S = 60                                   # 探测沙箱等进入 Running 的上限(平台实测建沙箱 0.1–0.6 秒)
PROBE_EXEC_TIMEOUT_S = 360
ADMIN_ERRORS = (pool.PoolError, KubeError, ValueError, KeyError)    # exec 不通 / 网关回的不是预期的 JSON
CAPACITY_WORDS = ("capacity", "insufficient", "no available", "no idle", "exhausted", "quota", "full")
SWAP_TRIES, SWAP_POLL_S = 12, 5                    # 网关滚动完以后,等 /admin/fuyao 报出新模板名:最多 12 次、每次隔 5 秒


def template_name(kind: str, tag: str) -> str:
    """gsdb-<runtime|import>-v<主>-<次>-<修订>,如 gsdb-runtime-v1-5-0。与 scripts/build-e2b-template.py 逐字同一规则。"""
    m = re.match(r"^agent-v(\d+)\.(\d+)\.(\d+)", tag)
    ver = "v%s-%s-%s" % m.groups() if m else re.sub(r"[^a-z0-9-]+", "-", tag.lower()).strip("-")
    return "gsdb-%s-%s" % ("runtime" if kind == "runtime" else "import", ver)


def templates_for(tag: str) -> Dict[str, str]:
    """种类 → 目标版本的模板名(与网关 /admin/fuyao 的 templates 同一个形状)。"""
    return {k: template_name(k, tag) for k in KINDS}


def build_hint(tag: str, kinds: List[str], arch: str, registry: str = "") -> str:
    """模板不可用时给运维的完整命令。"""
    cmd = "python3 %s --tag %s --arch %s%s" % (BUILD_SCRIPT, tag, arch, "" if len(kinds) == len(KINDS) else " --kind " + kinds[0])
    return ("请在平台的模板构建服务所在主机上(root,目标版本的代码目录里)执行:%s%s;"
            "那台主机拉不到镜像时,在能拉到的机器上按 docker/Dockerfile.e2b 构建好 OCI 包带过去,改执行:%s --from-oci <OCI 包>。"
            "模板就绪后在这里「从这一步续跑」" % (cmd, " --registry " + registry if registry else "", cmd))


# -- 在网关容器里跑的脚本(只用标准库;参数从标准输入给,结果打印成最后一行 JSON) -------------------------------

# 探测:按模板各建一台沙箱(请求格式同 gateway/opensandbox.py:image.uri = 模板名、entrypoint 占位、resourceLimits、
# extensions.sandboxGroup;不带 timeout),等进入 Running 后删除;最后按探测标记列举,剩下的全部删掉
PROBE = r'''
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
a = json.loads(sys.stdin.read() or "{}")
base = (a.get("api") or os.environ.get("GATEWAY_FUYAO_API") or "").rstrip("/")
hdr = {"Accept": "application/json", "Content-Type": "application/json"}
if os.environ.get("GATEWAY_FUYAO_API_KEY"):
    hdr["OPEN-SANDBOX-API-KEY"] = os.environ["GATEWAY_FUYAO_API_KEY"]
mk, mv = a["mark"]
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 同网关的客户端:不走环境变量里的代理
def call(method, path, body=None, timeout=30):
    req = urllib.request.Request(base + path, method=method, headers=hdr,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with direct.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()[:400]
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")
    except Exception as e:
        return -1, "%s" % e
def detail(b):
    if isinstance(b, dict):
        d = b.get("detail", b.get("message", b))
        return str(d.get("message") or d) if isinstance(d, dict) else str(d)
    return str(b)
def state(b):
    return str((b.get("status") or {}).get("state") or "") if isinstance(b, dict) else ""
def path(sid):
    return "/v1/sandboxes/" + urllib.parse.quote(sid, safe="")
def remove(sid):
    return call("DELETE", path(sid))[0] in (200, 202, 204, 404)
def probe(p):
    out = {"kind": p["kind"], "template": p["template"], "ok": False, "id": "", "state": "", "err": "", "deleted": False}
    t0 = time.time()
    body = {"image": {"uri": p["template"]}, "entrypoint": ["/bin/sh", "-c", "sleep infinity"],
            "resourceLimits": {"cpu": a.get("cpu") or os.environ.get("GATEWAY_FUYAO_CPU") or "2",
                               "memory": a.get("memory") or os.environ.get("GATEWAY_FUYAO_MEMORY") or "4Gi"},
            "metadata": {mk: mv}, "extensions": {"sandboxGroup": p["group"]}}
    code, r = call("POST", "/v1/sandboxes", body, timeout=60)
    out["code"] = code
    if code not in (200, 201, 202) or not isinstance(r, dict) or not r.get("id"):
        out["err"] = "建沙箱 HTTP %s:%s" % (code, detail(r)[:300])
        return out
    out["id"], st = str(r["id"]), state(r)
    end = t0 + float(a.get("wait_s") or 60)
    while st != "Running" and time.time() < end and not any(w in st.lower() for w in ("fail", "error", "terminat")):
        time.sleep(2)
        c2, r2 = call("GET", path(out["id"]))
        st = state(r2) if c2 == 200 else ("NotFound" if c2 == 404 else st)
        if c2 == 404:
            break
    out["state"], out["ok"], out["seconds"] = st, st == "Running", round(time.time() - t0, 1)
    if not out["ok"]:
        out["err"] = "沙箱建了,但 %d 秒内没有进入 Running(%s)" % (int(time.time() - t0), st or "无状态")
    out["deleted"] = remove(out["id"])
    return out
def leftovers():
    q = urllib.parse.urlencode([("metadata", "%s=%s" % (mk, mv)), ("page", "1"), ("pageSize", "100")])
    code, b = call("GET", "/v1/sandboxes?" + q)
    if code != 200 or not isinstance(b, dict):
        return None
    return [str(i.get("id")) for i in b.get("items") or [] if i.get("id") and (i.get("metadata") or {}).get(mk) == mv]
res, swept, left, err = [], 0, [], ""
if not base:
    err = "网关容器里没有 GATEWAY_FUYAO_API(gateway-config 的 FUYAO_API 没填)"
else:
    try:
        for p in a.get("probes") or []:
            res.append(probe(p))
    except Exception as e:
        err = "探测出错:%s" % e
    finally:
        ids = leftovers()
        if ids is None:
            left = [r["id"] for r in res if r["id"] and not r["deleted"]]
            err = err or ("按探测标记列举沙箱失败,没能核对是否删干净" if not left else "")
        else:
            swept = len(ids)
            left = [i for i in ids if not remove(i)]
print(json.dumps({"results": res, "swept": swept, "left": left, "err": err}, ensure_ascii=False))
'''

# 网关的 /admin/fuyao:不带 action 读摘要;带 action(delete 等)对一台沙箱做运维动作
ADMIN = r'''
import json, os, sys, urllib.request, urllib.error
a = json.loads(sys.stdin.read() or "{}")
url = "http://127.0.0.1:%s/admin/fuyao" % (os.environ.get("GATEWAY_LISTEN_PORT") or "8080")
hdr = {"X-Admin-Token": os.environ.get("GATEWAY_ADMIN_TOKEN", ""), "Content-Type": "application/json"}
if a.get("action"):
    doc = dict({"sandbox": a["sandbox"]}, **({"force": True} if a.get("force") is True else {}))
    req = urllib.request.Request(url + "/" + a["action"], data=json.dumps(doc).encode(), headers=hdr, method="POST")
else:
    req = urllib.request.Request(url, headers=hdr)
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=200) as r:
        print(json.dumps({"code": r.status, "body": json.loads(r.read() or b"{}")}))
except urllib.error.HTTPError as e:
    try:
        body = json.loads(e.read() or b"{}")
    except ValueError:
        body = {}
    print(json.dumps({"code": e.code, "body": body}))
'''


@dataclass
class FuyaoAdmin:
    """经 kubectl exec 调网关的 /admin/fuyao(选就绪、不在退出中的网关副本,失败换一个重试)。"""
    kube: object
    sleep: Callable[[float], None] = time.sleep

    def _call(self, action: str = "", sandbox: str = "", timeout: float = 240, force: bool = False) -> Tuple[int, dict]:
        payload = dict({"action": action, "sandbox": sandbox}, **({"force": True} if force else {}))
        doc = pool.GatewayAdmin(self.kube, self.sleep).run_script(ADMIN, payload, "/admin/fuyao", timeout)
        return int(doc["code"]), doc.get("body") or {}

    def summary(self) -> dict:
        code, body = self._call()
        if code != 200:
            raise pool.PoolError("/admin/fuyao 返回 %d:%s" % (code, body.get("error", "")))
        return body

    def delete(self, sandbox_id: str, force: bool = False) -> dict:
        """网关先收尾回写(fuyao-stop --release)再删,下一次请求按当前模板新建。回写没成功网关默认不删(409),
        force=True 才强删(会丢最后一次成功同步之后的改动)。"""
        code, body = self._call("delete", sandbox_id, force=force)
        if code != 200:
            raise pool.PoolError("/admin/fuyao 删除 %s 返回 %d:%s" % (sandbox_id, code, body.get("error", "")))
        return body


def probe_templates(kube, templates: Dict[str, str], cm: Dict[str, str], sleep: Callable[[float], None] = time.sleep,
                    wait_s: float = PROBE_WAIT_S) -> dict:
    """在网关容器里各建一台探测沙箱再删掉。cm:目标版本渲染出的 gateway-config(OpenSandbox 地址、沙箱组、资源字段)。"""
    probes = [{"kind": k, "template": templates[k], "group": str(cm.get(GROUP_KEYS[k]) or "")} for k in KINDS]
    payload = {"api": str(cm.get("FUYAO_API") or ""), "cpu": str(cm.get("FUYAO_CPU") or ""),
               "memory": str(cm.get("FUYAO_MEMORY") or ""), "mark": list(PROBE_MARK), "wait_s": wait_s, "probes": probes}
    return pool.GatewayAdmin(kube, sleep).run_script(PROBE, payload, "OpenSandbox(模板探测)", PROBE_EXEC_TIMEOUT_S)


def rendered_gateway_config(path: str) -> Dict[str, str]:
    for d in yaml.safe_load_all(pathlib.Path(path).read_text(encoding="utf-8")):
        if d and d.get("kind") == "ConfigMap" and (d.get("metadata") or {}).get("name") == "gateway-config":
            return {k: str(v) for k, v in (d.get("data") or {}).items()}
    raise StepFailed("渲染出的清单里没有 gateway-config:回到「配置迁移」重跑")


def _svc(ctx: Ctx):
    return ctx.services["svc"]


# -- 模板核对 ------------------------------------------------------------------------------------------

def _check_rendered(cm: Dict[str, str], want: Dict[str, str]) -> None:
    wrong = ["%s=%s(应为 %s)" % (TEMPLATE_KEYS[k], cm.get(TEMPLATE_KEYS[k]) or "空", v) for k, v in want.items()
             if cm.get(TEMPLATE_KEYS[k]) != v]
    if wrong:
        raise StepFailed("渲染出的 gateway-config 模板名与目标版本不符:%s。回到「配置迁移」重跑" % "、".join(wrong))
    empty = [GROUP_KEYS[k] for k in KINDS if not cm.get(GROUP_KEYS[k])]
    if empty:
        raise StepFailed("gateway-config 的 %s 没填(沙箱组由平台方给)" % "、".join(empty))


def step_template(ctx: Ctx) -> None:
    svc = _svc(ctx)
    tag, arch = ctx.plan["to_tag"], ctx.plan.get("arch") or "amd64"
    want = templates_for(tag)
    ctx.sub(0, "done", " / ".join(want[k] for k in KINDS))
    cm = rendered_gateway_config(ctx.shared()["rendered"])
    _check_rendered(cm, want)
    for k in (1, 2):
        ctx.sub(k, "running")
    try:
        got = probe_templates(svc.kube, want, cm, svc.sleep)
    except ADMIN_ERRORS as exc:
        raise StepFailed("探测模板失败:%s(探测沙箱带标记 %s=%s,续跑这一步会先清掉上次剩下的)" % ((exc,) + PROBE_MARK))
    by = {r.get("kind"): r for r in got.get("results") or []}
    bad = _record_probes(ctx, want, by, str(got.get("err") or ""))
    left = list(got.get("left") or [])
    ctx.share(templates={"want": want, "results": [dict(by.get(k) or {}, kind=k) for k in KINDS], "left": left,
                         "swept": got.get("swept", 0)})
    if left:
        ctx.sub(3, "failed", "%d 台没删掉" % len(left))
        raise StepFailed("探测沙箱没删干净:%s(metadata %s=%s);续跑这一步会再清一次,或在平台上手工删除"
                         % ("、".join(left), PROBE_MARK[0], PROBE_MARK[1]))
    ctx.sub(3, "failed" if got.get("err") else "done", "没能核对" if got.get("err") else "已清理 %d 台" % got.get("swept", 0))
    down = [k for k in bad if (by.get(k) or {}).get("code") == -1]
    if down:
        raise StepFailed("从网关连不上 OpenSandbox(gateway-config 的 FUYAO_API=%s):%s。先查平台服务与网关到平台的网络策略"
                         % (cm.get("FUYAO_API") or "空", (by.get(down[0]) or {}).get("err")))
    full = [k for k in bad if _no_capacity(by.get(k) or {})]
    if full:
        raise StepFailed("沙箱组没有空位,探测沙箱建不出来(%s):不是模板的问题,等有人空出来或请平台扩容后「从这一步续跑」"
                         % "、".join("%s %s" % (cm.get(GROUP_KEYS[k]), (by.get(k) or {}).get("err")) for k in full))
    if got.get("err"):
        raise StepFailed("%s;续跑这一步会再探测、再清理一次" % got["err"])
    if bad:
        registry = str(svc.cfg().get("registry.prefix") or "").rstrip("/")
        raise StepFailed("目标版本的模板不可用:%s。%s" % ("、".join("%s %s" % (KIND_NAMES[k], want[k]) for k in bad),
                                                   build_hint(tag, bad, arch, registry)))
    ctx.detail("模板可用:%s" % " / ".join(want[k] for k in KINDS))


def _no_capacity(r: dict) -> bool:
    """沙箱组满了(判断口径同 gateway/opensandbox.py 的 CapacityError)。"""
    return r.get("code") in (409, 429, 503) and any(w in str(r.get("err") or "").lower() for w in CAPACITY_WORDS)


def _record_probes(ctx: Ctx, want: Dict[str, str], by: Dict[str, dict], err: str) -> List[str]:
    """把每种模板的探测结果写进子步骤与日志,返回不可用的种类。"""
    bad = []
    for i, k in enumerate(KINDS):
        r = by.get(k) or {"err": err or "没有探测"}
        if r.get("ok"):
            ctx.sub(1 + i, "done", "可用 · %s 秒" % r.get("seconds", "?"))
            ctx.log("%s模板 %s 可用:探测沙箱 %s 进入 Running,%s" % (KIND_NAMES[k], want[k], r.get("id"),
                                                               "已删除" if r.get("deleted") else "删除没成功(清理时再删)"), "g")
        else:
            bad.append(k)
            ctx.sub(1 + i, "failed", str(r.get("err"))[:60])
            ctx.log("%s模板 %s 不可用:%s" % (KIND_NAMES[k], want[k], r.get("err")), "e")
    return bad


# -- 沙箱换版 ------------------------------------------------------------------------------------------

def step_sandbox(ctx: Ctx) -> None:
    svc = _svc(ctx)
    want = templates_for(ctx.plan["to_tag"])
    adm = FuyaoAdmin(svc.kube, svc.sleep)
    ctx.sub(0, "running")
    for attempt in range(SWAP_TRIES):
        ctx.check()
        try:
            summary = adm.summary()
        except ADMIN_ERRORS as exc:
            raise StepFailed("读网关的换版进度失败:%s" % exc)
        got = dict(summary.get("templates") or {})
        if all(got.get(k) == v for k, v in want.items()):
            break
        if attempt + 1 < SWAP_TRIES:
            svc.sleep(SWAP_POLL_S)                  # 刚滚动完,答话的可能还是没退干净的旧副本
    else:
        ctx.sub(1, "failed", "还是旧模板名")
        raise StepFailed("网关滚动完了,但还在用模板 %s(应为 %s):核对 gateway-config 的 %s 是否已应用、网关是否已重启"
                         % (got or "无", want, "、".join(TEMPLATE_KEYS.values())))
    boxes = summary.get("sandboxes") or []
    outdated = int(summary.get("outdated") or 0)
    ctx.sub(0, "done", "共 %d 台沙箱" % len(boxes))
    ctx.sub(1, "done", " / ".join(want[k] for k in KINDS))
    ctx.share(fuyao={"templates": got, "outdated": outdated, "total": len(boxes), "states": summary.get("states") or {}})
    if outdated:
        msg = "还有 %d 台旧版沙箱,闲置后自动换新" % outdated
        ctx.sub(2, "done", msg)
        ctx.log(msg + "(在用的不打断;没回写成功的不删)", "w")
        ctx.detail(msg)
        return
    ctx.sub(2, "done", "没有旧版沙箱")
    ctx.log("全部 %d 台沙箱已是新模板" % len(boxes), "g")
    ctx.detail("全部沙箱已是新模板(%d 台)" % len(boxes))


# -- 冒烟前:冒烟工号的旧版沙箱 ---------------------------------------------------------------------------

def refresh_smoke_user(kube, user: str, log: Callable[[str, str], None], sleep: Callable[[float], None] = time.sleep) -> List[str]:
    """冒烟工号自己的旧版诊断沙箱交给网关收尾删除,冒烟时网关按新模板新建;别人的沙箱、导入沙箱都不动。
    返回删掉的沙箱 ID。网关的 delete 动作先收尾回写再删;冒烟工号只有测试数据,回写没成功也要删(显式带 force,
    丢的是最后一次成功同步之后的改动),否则冒烟会跑在旧模板上。"""
    adm = FuyaoAdmin(kube, sleep)
    gone = []
    for s in adm.summary().get("sandboxes") or []:
        if s.get("user_id") != user or s.get("kind") != "runtime" or not s.get("outdated"):
            continue
        body = adm.delete(str(s.get("sandbox")), force=True)
        gone.append(str(s.get("sandbox")))
        log("冒烟工号 %s 的旧版%s沙箱 %s(模板 %s)已交网关收尾删除%s,冒烟时按新模板新建"
            % (user, KIND_NAMES.get(str(s.get("kind")), ""), s.get("sandbox"), s.get("template") or "?",
               "" if body.get("synced") is not False else "(收尾回写没成功)"), "w")
    return gone
