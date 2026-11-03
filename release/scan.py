"""发布前资源扫描(设计 §3):逐项检查,全部可用才允许开始发布。每项独立,一项出错不影响其他项。

模型、中间件、NAS 写入在**集群里面**测:跳板机连得上不代表 Pod 连得上(中间隔着网络策略)。已有池子时借管家容器
(它的网络策略放行模型与中间件,模型 Key 也在它的环境变量里 —— 探测脚本在容器里读,密钥不出容器);首次部署还没有池子,
这几项标「首次部署后核对」,由冒烟验证兜底。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional

from . import package, wldb
from .kube import Kube, KubeError
from .store import Store

OK, BAD, WARN = "ok", "bad", "warn"
NAMES = {"dir": "发布包目录", "registry": "内网镜像仓库", "k8s": "Kubernetes", "nas": "NAS", "model": "模型服务",
         "grmp": "中间件", "whitelist": "白名单核对", "sso": "统一认证", "capacity": "集群余量"}

MAX_SKEW = 1      # kubectl 与集群相差的小版本数,超过就提示(k8s 的版本偏差策略)

# 发版台要用到的权限(命名空间内)
VERBS = [("get", "configmaps"), ("patch", "configmaps"), ("get", "secrets"), ("create", "secrets"), ("patch", "deployments"),
         ("patch", "statefulsets"), ("patch", "pods"), ("delete", "pods"), ("create", "pods/exec"), ("create", "pods/portforward"),
         ("create", "jobs"), ("delete", "jobs"), ("patch", "networkpolicies"), ("delete", "deployments")]

# 模型:发一句最短的对话(与智能体同一个 OpenAI 兼容接口、同一个模型 ID)。只看 /models 会漏掉模型 ID 写错、
# 余额不足、服务方过载 —— 2026-11-03 演练 #8:扫描全绿,冒烟时服务方 503「Service is too busy」
PROBE_MODEL = r'''
import json, os, time, urllib.request, urllib.error
base = (os.environ.get("MODEL_BASE_URL") or "").rstrip("/")
body = json.dumps({"model": os.environ.get("MODEL_ID", ""), "max_tokens": 8,
                   "messages": [{"role": "user", "content": "ping"}]}).encode()
req = urllib.request.Request(base + "/chat/completions", data=body, headers={
    "Authorization": "Bearer " + os.environ.get("MODEL_API_KEY", ""), "Content-Type": "application/json"})
t, err = time.time(), ""
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
    try:
        b = json.loads(e.read() or b"{}"); err = str((b.get("error") or {}).get("message") or b.get("message") or "")[:160]
    except Exception:
        pass
except Exception as e:
    code, err = -1, str(e)[:160]
print(json.dumps({"code": code, "ms": int((time.time() - t) * 1000), "model": os.environ.get("MODEL_ID", ""), "err": err}))
'''
PROBE_GRMP = r'''
import json, os, socket, time
h, p = os.environ.get("GRMP_API_HOST", ""), int(os.environ.get("GRMP_API_PORT") or 0)
t = time.time()
try:
    socket.create_connection((h, p), 5).close(); ok = True
except Exception as e:
    ok = False
print(json.dumps({"ok": ok, "ms": int((time.time() - t) * 1000), "has_prefix": bool(os.environ.get("GRMP_PATH_PREFIX"))}))
'''
PROBE_NAS = r'''
import json, os, secrets
p = "/pool/users/.release-probe-" + secrets.token_hex(4)
try:
    open(p, "w").write("x"); ok = open(p).read() == "x"; os.unlink(p); err = ""
except Exception as e:
    ok, err = False, "%s" % e
st = os.statvfs("/pool/users")
print(json.dumps({"ok": ok, "err": err, "uid": os.getuid(), "free_gib": round(st.f_bavail * st.f_frsize / 2**30, 1)}))
'''


@dataclass
class Item:
    id: str
    name: str
    addr: str = ""
    st: str = "pending"
    msg: str = ""


@dataclass
class Scanner:
    store: Store
    kube: Kube
    releases: pathlib.Path
    target: str = ""                     # 目标版本标签:核它的 sha256
    log: Callable[[str], None] = lambda _m: None
    db_rows: Callable[[Dict[str, Any], str], List[tuple]] = wldb.rows     # 白名单只读账号;测试替换

    def cfg(self) -> Dict[str, Any]:
        return self.store.config()

    def run(self) -> List[Item]:
        items = []
        for fn in (self.dir, self.registry, self.k8s, self.nas, self.model, self.grmp, self.whitelist, self.sso, self.capacity):
            try:
                it = fn()
            except Exception as exc:            # noqa: BLE001  一项出错不影响其他项
                it = Item(fn.__name__, NAMES.get(fn.__name__, fn.__name__), st=BAD, msg="检查出错:%s" % exc)
            items.append(it)
            self.log("%s %s:%s" % ({"ok": "✓", "bad": "✕", "warn": "!"}.get(it.st, "?"), it.name, it.msg))
        status = BAD if any(i.st == BAD for i in items) else OK
        self.store.save_scan(status, [asdict(i) for i in items])
        return items

    # -- 各项 -------------------------------------------------------------------------------------

    def _steward(self) -> Optional[str]:
        for p in self.kube.list("pods", "app=gaussdb-agent,role=pool"):
            if any(c.get("name") == "steward" and c.get("ready") for c in (p.get("status") or {}).get("containerStatuses") or []):
                return p["metadata"]["name"]
        return None

    def _probe(self, script: str) -> Optional[dict]:
        pod = self._steward()
        if not pod:
            return None
        res = self.kube.exec(pod, "steward", ["python3", "-"], stdin=script, timeout=30)
        if res.code != 0:
            raise KubeError("在 %s 的管家里探测失败:%s" % (pod, (res.err.strip().splitlines() or ["?"])[-1]))
        return json.loads(res.out.strip().splitlines()[-1])

    def dir(self) -> Item:
        it = Item("dir", "发布包目录", str(self.releases))
        if not self.releases.is_dir():
            return Item("dir", "发布包目录", str(self.releases), BAD, "目录不存在或没有挂进容器")
        pkgs = package.discover(self.releases)
        if not pkgs:
            it.st, it.msg = BAD, "没有发现任何发布包(把离线包目录原样拷进来)"
            return it
        if self.target:
            pkg = next((p for p in pkgs if p.tag == self.target), None)
            if pkg is None:
                it.st, it.msg = BAD, "找不到 %s 的包" % self.target
                return it
            key = "sha:%s:%s" % (pkg.tag, ",".join("%s=%d" % (f.name, f.stat().st_mtime_ns) for f in pkg.files.values()))
            problems = self.store.kv_get(key)
            if problems is None:
                problems = package.verify(pkg)
                self.store.kv_set(key, problems)
            if problems or not pkg.ok:
                it.st, it.msg = BAD, ";".join(problems or pkg.problems)
                return it
        it.st, it.msg = OK, "发现 %d 个发布包%s" % (len(pkgs), (",%s 校验通过" % self.target) if self.target else "")
        return it

    def registry(self) -> Item:
        cfg = self.cfg()
        if cfg.get("registry.mode") == "preloaded":
            return Item("registry", "内网镜像仓库", "镜像已预置在节点上", WARN, "不推送(只用于测试环境)")
        prefix = str(cfg.get("registry.prefix") or "")
        if not prefix:
            return Item("registry", "内网镜像仓库", "", BAD, "资源配置里没填仓库地址")
        from .steps import registry_client
        registry_client(self.store, cfg).ping()
        return Item("registry", "内网镜像仓库", prefix, OK, "握手与认证通过(推送权限在镜像入库时核对)")

    def k8s(self) -> Item:
        ns = self.kube.namespace
        it = Item("k8s", "Kubernetes", "命名空间 %s" % ns)
        if self.kube.get("namespace", ns, namespaced=False) is None:
            it.st, it.msg = WARN, "命名空间还不存在:首次部署由发版台创建(需要权限),或请平台团队先建"
            return it
        missing = ["%s %s" % v for v in VERBS if not self.kube.can_i(*v)]
        if missing:
            it.st, it.msg = BAD, "缺权限:%s" % "、".join(missing)
            return it
        try:
            archs = sorted({(n["metadata"].get("labels") or {}).get("kubernetes.io/arch", "?")
                            for n in self.kube.list("nodes", namespaced=False)})
        except KubeError:
            archs = []
        if len(archs) > 1:
            it.st, it.msg = BAD, "节点架构混合(%s):第一版只支持单一架构" % "、".join(archs)
            return it
        if archs:
            self.store.kv_set("node_arch", archs[0])
        it.st, it.msg = OK, "权限完整%s" % (" · 节点 %s" % archs[0] if archs else "")
        v = self.kube.version()
        if "client" in v and "server" in v and abs(v["client"] - v["server"]) > MAX_SKEW:
            it.st = WARN
            it.msg += (" · kubectl 1.%d 与集群 1.%d 相差 %d 个小版本(官方只保证相差 %d 以内):"
                       "启动时用 --kubectl 换成集群配套的 kubectl" % (v["client"], v["server"], abs(v["client"] - v["server"]), MAX_SKEW))
        return it

    def nas(self) -> Item:
        pvc = self.kube.get("pvc", "nas")
        if pvc is None:
            return Item("nas", "NAS", "PVC nas", WARN, "PVC 还不存在:首次部署时建;PV / StorageClass 须由平台团队先准备")
        phase = (pvc.get("status") or {}).get("phase")
        if phase != "Bound":
            return Item("nas", "NAS", "PVC nas", BAD, "PVC 状态 %s(没有绑定到 PV)" % phase)
        r = self._probe(PROBE_NAS)
        if r is None:
            return Item("nas", "NAS", "PVC nas", OK, "已绑定(还没有池子,写入在首次部署后核对)")
        if not r["ok"]:
            return Item("nas", "NAS", "PVC nas", BAD, "uid %s 写入失败:%s · 请存储团队把 users/ 的属主改为 1000" % (r["uid"], r["err"]))
        return Item("nas", "NAS", "PVC nas", OK, "uid %s 可写 · 剩余 %s GiB" % (r["uid"], r["free_gib"]))

    def model(self) -> Item:
        r = self._probe(PROBE_MODEL)
        if r is None:
            return Item("model", "模型服务", "", WARN, "还没有池子:首次部署后由冒烟验证核对")
        addr, code, err = r.get("model") or "", r["code"], (" · 服务方原话:%s" % r["err"]) if r.get("err") else ""
        if code == 200:
            return Item("model", "模型服务", addr, OK, "从集群里对话通 · %d ms" % r["ms"])
        if code in (401, 403):
            return Item("model", "模型服务", addr, BAD, "认证失败(%d):模型 API Key 不对%s" % (code, err))
        if code == 402:
            return Item("model", "模型服务", addr, BAD, "余额不足(402)%s" % err)
        if code in (400, 404, 422):
            return Item("model", "模型服务", addr, BAD, "请求被拒(%d):多半是模型 ID 或地址不对%s" % (code, err))
        if code in (429, 500, 502, 503, 504, 529):
            return Item("model", "模型服务", addr, WARN,
                        "服务方现在忙或出错(%d):稍后再扫;这时发布,冒烟会失败、停在那一步等续跑%s" % (code, err))
        if code == -1:
            return Item("model", "模型服务", addr, BAD, "从集群里连不上:查网络策略的模型网段%s" % err)
        return Item("model", "模型服务", addr, BAD, "对话请求返回 %s%s" % (code, err))

    def grmp(self) -> Item:
        r = self._probe(PROBE_GRMP)
        if r is None:
            return Item("grmp", "中间件", "", WARN, "还没有池子:首次部署后由冒烟验证核对")
        if not r["has_prefix"]:
            return Item("grmp", "中间件", "", BAD, "agent-config 缺 GRMP_PATH_PREFIX")
        if not r["ok"]:
            return Item("grmp", "中间件", "", BAD, "从集群里连不上中间件端口:查网络策略的中间件网段")
        return Item("grmp", "中间件", "", OK, "从集群里连通 · %d ms(签名在冒烟时核对)" % r["ms"])

    def whitelist(self) -> Item:
        cfg = self.cfg()
        src = cfg.get("whitelist.source") or "csv"
        if src == "csv":
            text = self.store.kv_get("whitelist.export", "")
            n = max(0, len([l for l in text.splitlines() if l.strip()]) - 1)
            return Item("wl", "白名单核对", "导出文件", OK if n else WARN,
                        ("已上传 · %d 行" % n) if n else "还没上传导出文件(核对时需要)")
        addr = "只读账号 %s:%s/%s" % (cfg.get("whitelist.db.host") or "?", cfg.get("whitelist.db.port") or wldb.DEFAULT_PORT,
                                     cfg.get("whitelist.db.database") or "?")
        try:
            n = len(self.db_rows(cfg, self.store.secret("whitelist_password")))
        except wldb.DbError as exc:
            return Item("wl", "白名单核对", addr, BAD, str(exc))
        return Item("wl", "白名单核对", addr, OK if n else WARN,
                    ("连通 · 读到 %d 条" % n) if n else "连通,但 scene='AGENT' 一条都没有:白名单还没灌,或连的不是中间件库")

    def sso(self) -> Item:
        sec = self.kube.get("secret", "gateway-auth")
        user = str(self.cfg().get("smoke.user") or "")
        if sec is None:
            ok = bool(self.store.secret("trust_secret"))
            return Item("sso", "统一认证", "信任凭据", OK if ok else BAD, "首次部署时写入" if ok else "资源配置里没设置信任凭据")
        if not user:
            return Item("sso", "统一认证", "信任凭据", BAD, "资源配置里没填冒烟工号")
        if self.kube.get("secret", "grmp-token-%s" % user) is None:
            return Item("sso", "统一认证", "冒烟工号 %s" % user, BAD, "冒烟工号没有 grmp-token-%s(中间件令牌),请平台开通" % user)
        return Item("sso", "统一认证", "冒烟工号 %s" % user, OK, "信任凭据已设置 · 冒烟工号已开通")

    def capacity(self) -> Item:
        try:
            nodes = self.kube.list("nodes", namespaced=False)
        except KubeError:
            return Item("cap", "集群余量", "", WARN, "读不到节点(权限):跳过")
        mem = sum(_gib((n.get("status") or {}).get("allocatable", {}).get("memory", "0")) for n in nodes)
        return Item("cap", "集群余量", "%d 个节点" % len(nodes), OK,
                    "可分配内存 %.0f GiB · 按槽位在线替换不额外占用" % mem)


def _gib(q: str) -> float:
    units = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "K": 1e3, "M": 1e6, "G": 1e9}
    for u, m in units.items():
        if q.endswith(u):
            return float(q[: -len(u)]) * m / 2 ** 30
    try:
        return float(q) / 2 ** 30
    except ValueError:
        return 0.0
