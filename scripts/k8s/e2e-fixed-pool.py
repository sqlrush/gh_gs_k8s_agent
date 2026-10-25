#!/usr/bin/env python3
"""固定池化端到端(P7,2026-10-23):Mac 测试集群上 3 个 Pod × 3 个槽位,真 opencode,经池化模式的网关走完各场景。

    ~/p2venv/bin/python scripts/k8s/e2e-fixed-pool.py [--keep] [--only E3,E6]

前提:本地已载入 gaussdb-agent-runtime:pool-dev 与 gaussdb-agent-gateway:pool-dev;命名空间 gaussdb-agent 里有
model-api、agent-config、sandbox-control、gateway-auth、grmp-token 与 nas。

**不碰现有的网关与用户。** 另起一个池化模式的网关 gateway-pool(标签 app=agent-gateway-pool,现有 Service 选不到它),
工号每轮随机前缀(e<4 位>-NN),每人一个专用的假 GRMP 令牌(场景里不调中间件,只核对按人下发)。
跑完删掉 gateway-pool、本轮的令牌 Secret 与 NAS 目录,把池子 Pod 重建成全新的(--keep 保留现场)。
时长用测试开关缩短:闲置 120 秒转保留、满员时闲置 15 秒可被提前转保留、清扫 5 秒一轮、定期回写 15 秒。
秘密(信任密钥、运维密钥、控制密钥、令牌、口令)只在内存里,不打印。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
NS = "gaussdb-agent"
IMG_RT, IMG_GW = "gaussdb-agent-runtime:pool-dev", "gaussdb-agent-gateway:pool-dev"
PORT_A, PORT_B = 18191, 18192
IDLE, EARLY, SWEEP, SYNC = 120, 15, 5, 15
RUN = "e" + secrets.token_hex(2)
RESULTS = []
_PF = []


def U(n: int) -> str:
    return "%s-%02d" % (RUN, n)


# --- 外部命令 ------------------------------------------------------------------------------------------

def K(*args, input_=None, check=True, timeout=300) -> str:
    cmd = ["kubectl", "--context", CTX, "-n", NS] + list(args)
    p = subprocess.run(cmd, input=input_, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("kubectl %s 失败:%s" % (" ".join(args[:3]), p.stderr.strip()[:300]))
    return p.stdout


def secret(name: str, key: str) -> str:
    return base64.b64decode(K("get", "secret", name, "-o", "jsonpath={.data.%s}" % key)).decode()


def kexec(pod: str, container: str, script: str, check=False) -> str:
    return K("exec", pod, "-c", container, "--", "sh", "-c", script, check=check, timeout=120)


def apply(obj: dict) -> None:
    K("apply", "-f", "-", input_=json.dumps(obj))


def log(msg: str) -> None:
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def record(code: str, name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((code, name, ok, detail))
    log("%s %s %s %s" % ("✓" if ok else "✗", code, name, detail))
    return ok


def wait_for(cond, timeout: float, step: float = 1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = cond()
        if got:
            return got
        time.sleep(step)
    return None


# --- 网关 ---------------------------------------------------------------------------------------------

def gw(user: str, path: str, method: str = "GET", body=None, accept: str = "application/json",
       port: int = PORT_A, timeout: float = 150):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method)
    for k, v in {"X-Agent-User": user, "X-Agent-Trust": TRUST, "Accept": accept,
                 "Content-Type": "application/json"}.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as exc:     # 流被中途切断也算一种结果
        return 0, {}, str(exc)


def admin(path: str = "/admin/pool", body=None, port: int = PORT_A):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                 method="POST" if body is not None else "GET")
    req.add_header("X-Admin-Token", ADMIN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def slots():
    _c, s = admin()
    return [dict(sl, pod=p["name"]) for p in s.get("pods", []) for sl in p["slots"]]


def where(user: str):
    for s in slots():
        if s["user_id"] == user and s["state"] not in ("evicting",):
            return s["pod"], s["slot"], s["state"]
    return None


def ensure_active(user: str):
    """让他进来(交互请求会分配 / 恢复),返回 (Pod, 槽位, 状态)。"""
    p = where(user)
    if p and p[2] == "active":
        return p
    titles(user)
    return wait_for(lambda: (lambda w: w if w and w[2] == "active" else None)(where(user)), 60)


def docker_kill(pod: str, container: str) -> None:
    """从外面 SIGKILL 一个容器(节点故障 / OOM 的样子):容器里的 1 号进程没有任何收尾的机会。"""
    cid = K("get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[?(@.name==\"%s\")].containerID}" % container)
    subprocess.run(["docker", "kill", cid.split("://", 1)[-1]], capture_output=True, check=True)


def steward_logs() -> str:
    return "".join(K("logs", "agent-pool-%d" % i, "-c", "steward", "--tail=300", check=False) for i in range(3))


def new_session(user: str, title: str, port: int = PORT_A):
    code, _h, body = gw(user, "/session", "POST", {"title": title}, port=port)
    try:
        return code, json.loads(body).get("id")
    except ValueError:
        return code, None


def titles(user: str, port: int = PORT_A):
    code, _h, body = gw(user, "/session", port=port)
    try:
        got = json.loads(body) if code == 200 else []
    except ValueError:
        got = []
    return code, [s.get("title") for s in got if isinstance(s, dict)] if isinstance(got, list) else []


def port_forward(target: str, local: int) -> None:
    # 按本地端口清:上一轮被中断时留下的 port-forward 指着已经不在的 Pod,还占着这个端口
    subprocess.run(["pkill", "-f", "port-forward .* %d:8080" % local], capture_output=True)
    time.sleep(0.5)
    p = subprocess.Popen(["kubectl", "--context", CTX, "-n", NS, "port-forward", target, "%d:8080" % local],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _PF.append(p)
    wait_for(lambda: gw("healthz", "/healthz", port=local)[0] == 200, 30, 0.5)


def gateway_pods():
    out = K("get", "pods", "-l", "app=agent-gateway-pool", "--field-selector=status.phase=Running",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{' '}{.metadata.deletionTimestamp}{'\\n'}{end}")
    return sorted(line.split()[0] for line in out.splitlines() if line.strip() and len(line.split()) == 1)


def forward_gateways() -> None:
    names = wait_for(lambda: (lambda n: n if len(n) == 2 else None)(gateway_pods()), 120)
    port_forward("pod/" + names[0], PORT_A)
    port_forward("pod/" + names[1], PORT_B)


def container_id(pod: str, slot: int) -> str:
    return K("get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[?(@.name==\"slot-%d\")].containerID}" % slot)


def restarts(pod: str, slot: int) -> int:
    out = K("get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[?(@.name==\"slot-%d\")].restartCount}" % slot)
    return int(out or 0)


def wait_pod_ready(name: str, timeout: float = 300, slots_too: bool = True) -> bool:
    """StatefulSet 重建同名 Pod 之前有个空档:先等它出现,再等就绪,再等管家和各槽位握完手。"""
    ok = wait_for(lambda: K("get", "pod", name, "-o", "jsonpath={.status.containerStatuses[*].ready}",
                            check=False).split().count("true") >= 4, timeout, 2)
    if not slots_too:
        return bool(ok)
    return bool(ok) and bool(wait_for(lambda: (lambda ss: ss and all(s["state"] not in ("booting",) for s in ss))(
        [s for s in slots() if s["pod"] == name]), 90, 2))


def recreate_pool() -> None:
    """删掉池子的 Pod 让 StatefulSet 重建,按 UID 换没换判断(kubectl delete --wait 偶尔漏掉删除事件、一直不返回)。"""
    uids = lambda: dict(line.split() for line in K("get", "pod", "-l", "app=gaussdb-agent,role=pool", "-o",   # noqa: E731
                        "jsonpath={range .items[*]}{.metadata.name} {.metadata.uid}{'\\n'}{end}").splitlines() if line.strip())
    old = uids()
    K("delete", "pod", "-l", "app=gaussdb-agent,role=pool", "--wait=false")
    wait_for(lambda: (lambda now: len(now) == len(old) and not set(now.values()) & set(old.values()))(uids()), 400, 3)
    for name in old:
        wait_pod_ready(name, slots_too=False)          # 这时网关可能还没起来;槽位握手由 E1 等


# 槽位里 opencode 进程的 /proc 目录(按命令行开头认;跳过执行这段的 shell 自己 —— 它的命令行里也有这个词)
_OC_PROC = ('for d in /proc/[0-9]*; do [ "${d#/proc/}" = "$$" ] && continue; '
            'case "$(tr \'\\0\' \' \' < $d/cmdline 2>/dev/null)" in "opencode serve"*) %s; break;; esac; done')


def touch_all(users):
    for u in users:
        gw(u, "/session")


# --- 部署 ----------------------------------------------------------------------------------------------

def deploy() -> None:
    log("== 部署:池子 3×3(测试时长)+ 池化模式网关 gateway-pool ×2")
    py = sys.executable
    out = subprocess.run([py, str(ROOT / "k8s" / "tools" / "render-pool.py"), "--image", IMG_RT,
                          "--image-pull-policy", "Never", "--pool-size", "3", "--slots-per-pod", "3",
                          "--slot-cpu-request", "50m", "--slot-memory-request", "256Mi",
                          # 测试环境的出站目标(k8s/local/networkpolicy-local.yaml 同一组):宿主上的中间件 mock、模型、Ollama
                          "--grmp-cidr", "0.250.250.254/32", "--grmp-port", "8781",
                          "--model-cidr", "0.0.0.0/0", "--model-ports", "443", "--egress", "0.250.250.254/32:11434",
                          "--test-override", "STATE_SYNC_SECONDS=%d" % SYNC,
                          "--test-override", "POOL_EVICT_TIMEOUT_SECONDS=150"],
                         capture_output=True, text=True, check=True).stdout
    K("apply", "-f", "-", input_=out)
    recreate_pool()
    import yaml
    docs = list(yaml.safe_load_all((ROOT / "k8s" / "base" / "gateway.yaml").read_text(encoding="utf-8")))
    dep = next(d for d in docs if d and d["kind"] == "Deployment")
    svc = next(d for d in docs if d and d["kind"] == "Service")
    lab = {"app": "agent-gateway-pool"}
    dep["metadata"].update(name="gateway-pool", labels=lab)
    dep["spec"]["selector"] = {"matchLabels": lab}
    dep["spec"]["template"]["metadata"]["labels"] = lab
    dep["spec"]["replicas"] = 2
    c = dep["spec"]["template"]["spec"]["containers"][0]
    c["image"], c["imagePullPolicy"] = IMG_GW, "Never"
    force = {"GATEWAY_RUNTIME_MODE": "pool", "POOL_TEST_SHORT_TIMERS": "1", "GATEWAY_POOL_IDLE_SECONDS": str(IDLE),
             "GATEWAY_POOL_EARLY_IDLE_SECONDS": str(EARLY), "GATEWAY_POOL_SWEEP_SECONDS": str(SWEEP),
             "GATEWAY_POOL_WAIT_SECONDS": "90", "GATEWAY_POOL_FREE_SLOTS": "1"}
    c["env"] = [e for e in c["env"] if e["name"] not in force] + [{"name": k, "value": v} for k, v in force.items()]
    svc["metadata"].update(name="gateway-pool", labels=lab)
    svc["spec"]["selector"] = lab
    apply(dep)
    apply(svc)
    K("rollout", "restart", "deploy/gateway-pool")
    K("rollout", "status", "deploy/gateway-pool", "--timeout=180s", timeout=200)
    K("wait", "--for=condition=Ready", "pod", "-l", "app=gaussdb-agent,role=pool", "--timeout=240s", timeout=260)
    e2b_network_policy()
    forward_gateways()
    # 两个网关副本都看得到全部管家再开始(网关刚起来时,它第一次读池子可能早于上面那条放行策略生效)
    for port in (PORT_A, PORT_B):
        wait_for(lambda: (lambda r: r[0] == 200 and len(r[1].get("pods", [])) == 3
                          and all(p["reachable"] for p in r[1]["pods"]))(admin(port=port)), 90, 2)
    for n in range(1, 13):
        apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "grmp-token-" + U(n), "namespace": NS,
                                                                  "labels": {"e2e-run": RUN}},
               "type": "Opaque", "stringData": {"GRMP_AUTH_TOKEN": TOKENS[U(n)]}})


_PROBE = ("import socket,sys\ns=socket.socket(); s.settimeout(3)\n"
          "try:\n s.connect((sys.argv[1], int(sys.argv[2]))); print('connected')\nexcept Exception as e:\n print('blocked')")


def e2b_network_policy() -> None:
    """OrbStack 是执行网络策略的(k3s 自带的策略控制器):pool-policy 只放行网关(app=agent-gateway)。
    测试网关的标签不同,先验证它连不上,再加一条只给本轮用的放行策略。"""
    ip = K("get", "pod", "agent-pool-0", "-o", "jsonpath={.status.podIP}")
    probe = lambda dep, port: K("exec", "deploy/" + dep, "--", "python3", "-c", _PROBE, ip, str(port),   # noqa: E731
                                check=False).strip()
    outsider = [probe("gateway-pool", port) for port in (4098, 4100, 4101)]
    real = probe("gateway", 4098)
    record("E2b", "网络策略:不是网关的 Pod 连不上管家与槽位端口,网关连得上",
           outsider == ["blocked"] * 3 and real == "connected", "测试网关 %s / 网关 %s" % (outsider, real))
    ports = [{"port": 4098, "protocol": "TCP"}] + [{"port": 4100 + 10 * i + k, "protocol": "TCP"}
                                                   for i in range(3) for k in (0, 1)]
    apply({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
           "metadata": {"name": "e2e-gateway-to-pool", "namespace": NS, "labels": {"e2e-run": RUN}},
           "spec": {"podSelector": {"matchLabels": {"app": "gaussdb-agent", "role": "pool"}}, "policyTypes": ["Ingress"],
                    "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "agent-gateway-pool"}}}],
                                 "ports": ports}]}})
    wait_for(lambda: probe("gateway-pool", 4098) == "connected", 30)


def cleanup(keep: bool) -> None:
    for p in _PF:
        p.terminate()
    if keep:
        log("保留现场(--keep):gateway-pool、本轮令牌与 NAS 目录都还在,工号前缀 %s" % RUN)
        return
    log("== 清理")
    K("delete", "deploy/gateway-pool", "svc/gateway-pool", "--ignore-not-found", check=False)
    K("delete", "secret,networkpolicy", "-l", "e2e-run=%s" % RUN, check=False)
    steward = "agent-pool-0"
    kexec(steward, "steward", "rm -rf /pool/users/%s-*" % RUN)
    recreate_pool()


# --- 场景 ----------------------------------------------------------------------------------------------

def e1_deployed():
    ok = wait_for(lambda: (lambda s: s if len(s) == 9 and all(x["state"] == "free" for x in s) else None)(slots()), 120)
    record("E1", "部署:3 个 Pod × 3 个槽位全部空闲", bool(ok), "%d 个槽位" % len(ok or []))


def e2_isolation():
    x = lambda cmd: kexec("agent-pool-0", "slot-1", cmd).strip()    # noqa: E731
    checks = {
        "uid 2001": x("id -u") == "2001",
        "无 /pool": "No such file" in x("ls /pool 2>&1"),
        "/nas 只有 kb": x("ls /nas") == "kb",
        "环境无密钥": x("env | grep -c -E 'CONTROL_KEY|GRMP_AUTH_TOKEN|OPENCODE_SERVER_PASSWORD'") == "0",
        "管家接口 401": x("curl -s -o /dev/null -w %{http_code} http://127.0.0.1:4098/pool/status") == "401",
        "抢不到 slot-0 端口": "Address already in use" in x(
            "python3 -c 'import socket;s=socket.socket();s.bind((\"0.0.0.0\",4100))' 2>&1"),
        "指令目录只读": "Read-only" in x("touch /run/sandbox/x 2>&1"),
    }
    bad = [k for k, v in checks.items() if not v]
    record("E2", "槽位内隔离", not bad, "失败:%s" % bad if bad else "%d 项" % len(checks))


def e3_e18_exclusive_and_race():
    res = {}

    def go(n, port):
        res[n] = new_session(U(n), "s-%d" % n, port=port)
    t0 = time.time()
    ts = [threading.Thread(target=go, args=(1, PORT_A)), threading.Thread(target=go, args=(2, PORT_B))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    took = time.time() - t0
    go(3, PORT_A)
    pods = [where(U(n)) for n in (1, 2, 3)]
    ok = all(res[n][0] == 200 and res[n][1] for n in (1, 2, 3))
    record("E3", "前 3 人各占一个 Pod(新人约 %.1f 秒)" % took,
           ok and len({p[0] for p in pods if p}) == 3, str([p[:2] if p else None for p in pods]))
    record("E18", "两个网关副本同时给两个新人分配,落到不同的 Pod",
           bool(pods[0] and pods[1]) and pods[0][0] != pods[1][0])


def e8_move_to_empty_pod():
    home = where(U(1))
    admin("/admin/pool/retain", {"pod": home[0], "slot": home[1]})
    new_session(U(4), "s-4")                                   # 唯一的空 Pod 是 U1 的:U4 进去
    p4 = where(U(4))
    p2 = where(U(2))
    admin("/admin/pool/retain", {"pod": p2[0], "slot": p2[1]})     # U2 的 Pod 空出来
    code, ts = titles(U(1))                                   # U1 回来:家里有人在用、别处有空 Pod → 换过去
    now = where(U(1))
    old_freed = wait_for(lambda: next((s for s in slots() if (s["pod"], s["slot"]) == home[:2]
                                       and s["state"] == "free"), None), 90)
    record("E8", "回来时家里有人在用、别处有空 Pod:换到空 Pod,旧的保留槽位腾挪核验后空闲",
           code == 200 and "s-1" in ts and p4[0] == home[0] and now and now[0] == p2[0] and bool(old_freed),
           "U4 进了 %s,U1 %s → %s" % (p4[0], home[:2], now[:2] if now else None))


def e4_fill_and_balance():
    ensure_active(U(2))                                       # U2 先回来(恢复):再晚,新人会把他的保留槽位腾挪掉
    for n in range(5, 10):
        new_session(U(n), "s-%d" % n)
    active = [s for s in slots() if s["state"] == "active"]
    per_pod = {}
    for s in active:
        per_pod[s["pod"]] = per_pod.get(s["pod"], 0) + 1
    record("E4", "第 4–9 人按「在用最少」分到各 Pod,9 个槽位全在用",
           len(active) == 9 and max(per_pod.values()) - min(per_pod.values()) <= 1, str(per_pod))


def e5_full_then_early_retain():
    users = [s["user_id"] for s in slots() if s["state"] == "active"]
    touch_all(users)
    time.sleep(SWEEP + 1)                                      # 活动报到管家
    code, h, body = gw(U(10), "/", accept="text/html")
    record("E5a", "满员:浏览器看到每 30 秒自动重试的等待页", code == 503 and 'content="30"' in body
           and h.get("Retry-After") == "30", "HTTP %d" % code)
    time.sleep(EARLY + SWEEP + 2)                              # 大家都闲置超过 15 秒了
    t0 = time.time()
    code, sid = new_session(U(10), "s-10")
    took = time.time() - t0
    logs = K("logs", "-l", "app=agent-gateway-pool", "--tail=400", check=False)
    line = next((l for l in logs.splitlines() if "预留给 %s" % U(10) in l), "")
    victim = line.split("满员,", 1)[-1].split(" ", 1)[0] if line else ""
    # 被提前转保留的那位的槽位随后就腾挪给了等待的人,所以不看「还剩保留槽位」,看他不再在用、等的人在用
    record("E5b", "满员时提前转保留一位闲置者,腾出的槽位预留给等待的人(%.1f 秒)" % took,
           code == 200 and bool(sid) and bool(victim) and (where(victim) or (0, 0, ""))[2] != "active"
           and (where(U(10)) or (0, 0, ""))[2] == "active", "提前转保留的是 %s" % victim)


def e6_idle_retain_and_resume():
    u = U(10)
    p = where(u)
    ok_ret = wait_for(lambda: (where(u) or (None, None, ""))[2] == "retained", IDLE + 4 * SWEEP + 30, 2)
    t0 = time.time()
    code, ts = titles(u)
    took = time.time() - t0
    record("E6", "闲置 %d 秒转保留;回来恢复(%.1f 秒),对话还在" % (IDLE, took),
           bool(ok_ret) and code == 200 and "s-10" in ts and where(u)[:2] == p[:2], "槽位 %s" % (p[:2],))


def e7_sse_ends_and_does_not_resume():
    u = U(10)
    p = where(u)
    ended = threading.Event()

    def stream():
        gw(u, "/global/event", accept="text/event-stream", timeout=120)
        ended.set()
    threading.Thread(target=stream, daemon=True).start()
    time.sleep(3)
    admin("/admin/pool/retain", {"pod": p[0], "slot": p[1]})
    gone = ended.wait(30)
    c1 = gw(u, "/global/event", accept="text/event-stream")[0]
    c2 = gw(u, "/api/health")[0]
    time.sleep(SWEEP + 2)
    record("E7", "转保留时事件流结束;事件流与探活不会把槽位拉回来",
           gone and c1 == 503 and c2 == 503 and where(u)[2] == "retained", "重连 %d / 探活 %d" % (c1, c2))


def e9_stale_gen():
    u = U(10)
    p = where(u)
    kexec(p[0], "steward", "cd /opt/agent && python3 -c \"import fdtree,poolsync; r=fdtree.open_root('/pool/users');"
                           " u=poolsync.open_user(r,'%s'); poolsync.gen_rotate(u)\"" % u)
    code, ts = titles(u)
    now = where(u)
    freed = wait_for(lambda: next((s for s in slots() if (s["pod"], s["slot"]) == p[:2] and s["user_id"] != u), None), 90)
    record("E9", "别处取过独占(版本令牌换了):保留副本作废,从 NAS 重新载入,旧槽位腾挪",
           code == 200 and "s-10" in ts and bool(now) and bool(freed), "%s → %s" % (p[:2], now[:2] if now else None))


def e24_e25_tokens():
    u = U(3)
    p = ensure_active(u)
    env_hash = kexec(p[0], "slot-%d" % p[1], _OC_PROC % (
        "tr '\\0' '\\n' < $d/environ | sed -n 's/^GRMP_AUTH_TOKEN=//p' | head -1 | tr -d '\\n' | sha256sum | cut -c1-16"
    )).strip()
    want = hashlib.sha256(TOKENS[u].encode()).hexdigest()[:16]
    record("E24", "按人下发 GRMP 令牌:opencode 拿到的就是本人 Secret 里的那个", env_hash == want)
    code, _h, body = gw(u, "/reports/whoami.json")
    record("E25", "报告经网关带报告令牌转到本人槽位", code == 200 and json.loads(body or "{}").get("user_id") == u)


def e19_gateway_restart():
    u = U(3)
    before = ensure_active(u)
    K("rollout", "restart", "deploy/gateway-pool")
    K("rollout", "status", "deploy/gateway-pool", "--timeout=180s", timeout=200)
    time.sleep(3)
    forward_gateways()
    code, ts = titles(u)
    record("E19", "网关重启后在用的人照常转发(幂等取回凭证),槽位不变", code == 200 and "s-3" in ts
           and where(u)[:2] == before[:2])


def e12_port_squatting():
    victim = next((s for s in slots() if s["state"] == "active"), None)
    if victim is None:
        titles(U(3))
        victim = where(U(3))
        victim = {"pod": victim[0], "slot": victim[1], "user_id": U(3)}
    pod = victim["pod"]
    same = [s for s in slots() if s["pod"] == pod and s["slot"] != victim["slot"] and s["state"] in ("free", "retained")]
    if not same:
        return record("E12", "端口抢占", False, "同 Pod 没有空闲 / 保留的槽位可以当冒充者")
    squatter = same[0]["slot"]
    vport = 4100 + 10 * victim["slot"]
    r0 = restarts(pod, squatter)
    kexec(pod, "slot-%d" % squatter, "nohup python3 -c \"import socket,time\nwhile True:\n try:\n  s=socket.socket();"
          "s.bind(('0.0.0.0',%d));s.listen(4);time.sleep(600)\n except OSError:\n  s.close();time.sleep(0.002)\" "
          ">/dev/null 2>&1 &" % vport)
    time.sleep(1)
    kexec(pod, "slot-%d" % victim["slot"], _OC_PROC % "kill -9 ${d#/proc/}")
    seen = wait_for(lambda: "冒充" in K("logs", pod, "-c", "steward", "--tail=50", check=False), 30)
    evicted = wait_for(lambda: restarts(pod, squatter) > r0, 90, 2)
    back = wait_for(lambda: next((s for s in slots() if s["pod"] == pod and s["slot"] == victim["slot"]
                                  and s["listener"] == "ok"), None), 90, 2)
    if not seen:
        return record("E12", "端口抢占:占位监听抢先回到端口,冒充者没抢到(防线一生效)", bool(back),
                      "槽位 %s/%d 端口 %d" % (pod, victim["slot"], vport))
    record("E12", "端口抢占被识别并定位到肇事槽位,肇事槽位被腾挪,端口回到本人", bool(evicted) and bool(back),
           "冒充者 slot-%d 重启 %s" % (squatter, bool(evicted)))


def e26_leftovers_and_e14_backoff():
    u = U(5)
    p = ensure_active(u)
    pod, slot = p[0], p[1]
    c = "slot-%d" % slot
    kexec(pod, c, "echo '[core] fsmonitor = touch /tmp/pwned' > /data/home/.gitconfig; echo 'import os' > /tmp/evil.pth;"
                  " echo x > /dev/shm/leak; echo planted > /data/config/opencode/AGENTS.md")
    admin("/admin/pool/retain", {"pod": pod, "slot": slot})
    t0 = time.time()
    admin("/admin/pool/evict", {"pod": pod, "slot": slot})
    freed = wait_for(lambda: next((s for s in slots() if (s["pod"], s["slot"]) == (pod, slot) and s["state"] == "free"),
                                  None), 120, 1)
    t1 = time.time() - t0
    left = kexec(pod, c, "ls /data/home/.gitconfig /tmp/evil.pth /dev/shm/leak 2>/dev/null; "
                         "grep -l planted /data/config/opencode/AGENTS.md 2>/dev/null").strip()
    record("E26", "上一个人植入的 .gitconfig / .pth / shm / AGENTS.md,腾挪后都不在了", bool(freed) and not left,
           "残留:%s" % left if left else "")
    t0 = time.time()
    admin("/admin/pool/evict", {"pod": pod, "slot": slot})
    again = wait_for(lambda: next((s for s in slots() if (s["pod"], s["slot"]) == (pod, slot) and s["state"] == "free"),
                                  None), 200, 1)
    t2 = time.time() - t0
    record("E14", "同一槽位连续两次腾挪:第一次 %.0f 秒,第二次 %.0f 秒(kubelet 重启退避)" % (t1, t2), bool(again))


def e10_free_slots():
    def check():
        pods = {}
        for s in slots():
            pods.setdefault(s["pod"], []).append(s)
        full = [p for p, ss in pods.items() if not any(x["state"] == "free" for x in ss)
                and any(x["state"] == "retained" and not x["dirty"] for x in ss)]
        return not full
    ok = wait_for(check, 120, 3)
    record("E10", "每个 Pod 至少留 1 个空闲槽位(后台腾挪最久没来的保留者)", bool(ok))


def e15_pod_delete():
    u = U(6)
    p = ensure_active(u)
    new_session(u, "before-delete")
    K("delete", "pod", p[0], "--wait=true", timeout=300)
    wait_pod_ready(p[0])
    code, ts = titles(u)
    record("E15", "Pod 正常删除:在用的人先完整写回;重建后再来,对话都在", code == 200 and "before-delete" in ts,
           "%s → %s" % (p[:2], where(u)[:2] if where(u) else None))


def e16_ungraceful():
    """节点故障 / OOM 的样子:从外面 SIGKILL 槽位容器(--force 删 Pod 在这个集群上仍会先发停止信号,测不出来)。"""
    u = U(7)
    p = ensure_active(u)
    new_session(u, "synced")
    time.sleep(SYNC + 8)                                      # 等一次定期回写
    new_session(u, "too-late")
    docker_kill(p[0], "slot-%d" % p[1])
    gone = wait_for(lambda: (where(u) or (None, None, "gone"))[2] != "active", 60, 2)
    got = wait_for(lambda: (lambda r: r if r[0] == 200 else None)(titles(u)), 60, 3)   # 浏览器也会重试
    code, ts = got if got else (0, [])
    logs = steward_logs()
    record("E16", "槽位容器被 SIGKILL:管家发现重启过、放掉独占;再来时从 NAS 载入,同步周期之前的对话都在,提示上次非正常退出",
           bool(gone) and code == 200 and "synced" in ts and "非优雅" in logs and "重启过" in logs,
           "too-late %s" % ("也在" if "too-late" in ts else "丢了(预期内,在最后一个同步周期里)"))


def e17_steward_killed():
    """管家容器被 SIGKILL:网关直连槽位,对话不断;管家重启后接着管这个槽位。"""
    u = U(8)
    p = ensure_active(u)
    titles(u)                                                 # 让网关 A 缓存他的凭证
    r0 = int(K("get", "pod", p[0], "-o", "jsonpath={.status.containerStatuses[?(@.name==\"steward\")].restartCount}") or 0)
    docker_kill(p[0], "steward")
    codes = []
    for _ in range(10):
        codes.append(titles(u)[0])
        time.sleep(1)
    back = wait_for(lambda: int(K("get", "pod", p[0], "-o", "jsonpath={.status.containerStatuses[?(@.name==\"steward\")]"
                                  ".restartCount}") or 0) > r0 and where(u) == p, 120, 2)
    record("E17", "管家容器被 SIGKILL:对话照常(%d/%d 个请求成功),管家重启后接着管这个槽位" % (codes.count(200), len(codes)),
           codes.count(200) == len(codes) and bool(back), "槽位 %s" % (p[:2],))


def e12b_squat_during_restart():
    """槽位被腾挪、容器重启的空档里,同 Pod 的另一个槽位先占了它的端口。新起的 1 号进程占不到;
    管家认出冒充者并定位到肇事槽位,网关把肇事槽位腾挪;端口被原主每轮重试占回来,核验通过。"""
    pod = "agent-pool-1"
    ss = [s for s in slots() if s["pod"] == pod]
    victim = next((s for s in ss if s["state"] == "free"), None)
    squat = next((s for s in ss if victim and s["slot"] != victim["slot"] and s["state"] in ("free", "retained")
                  and not s["dirty"]), None)
    if not victim or not squat:
        return record("E12b", "重启空档里的端口抢占", False, "%s 里没有两个可用的槽位:%s" % (pod, [x["state"] for x in ss]))
    a, b = victim["slot"], squat["slot"]
    pa, pb = 4100 + 10 * a, 4101 + 10 * a
    rb = restarts(pod, b)
    kexec(pod, "slot-%d" % b, "nohup python3 -c \"import socket,time\nheld={}\nwhile len(held)<2:\n for p in (%d,%d):\n"
          "  if p in held: continue\n  s=socket.socket()\n  try:\n   s.bind(('0.0.0.0',p));s.listen(4);held[p]=s\n"
          "  except OSError:\n   s.close()\n time.sleep(0.002)\ntime.sleep(900)\" >/dev/null 2>&1 &" % (pa, pb))
    admin("/admin/pool/evict", {"pod": pod, "slot": a})
    # 判据看管家:「槽位 a 的端口上有外来监听者」= 冒充者确实占住了原主的端口;「槽位 b … 冒充」= 定位到了肇事槽位。
    # (槽位自己的「占不住端口」那行会随后续的腾挪换到上一个容器实例的日志里,不拿它当判据)
    steward = lambda: K("logs", pod, "-c", "steward", "--tail=120", check=False)     # noqa: E731
    blocked = wait_for(lambda: "槽位 %d 的端口上有外来监听者" % a in steward(), 120, 2)
    flagged = wait_for(lambda: "槽位 %d 在槽位 %d 的端口上监听(冒充)" % (b, a) in steward(), 60, 2)
    evicted = wait_for(lambda: restarts(pod, b) > rb, 120, 2)
    healed = wait_for(lambda: next((s for s in slots() if s["pod"] == pod and s["slot"] == a and s["state"] == "free"
                                    and s["listener"] == "ok"), None), 400, 3)
    record("E12b", "重启空档里被抢端口:认出冒充者并定位到 slot-%d,肇事槽位被腾挪,原主占回端口后核验通过" % b,
           bool(blocked) and bool(flagged) and bool(evicted) and bool(healed),
           "端口被占 %s / 定位 %s / 肇事者重启 %s / 恢复 %s" % (bool(blocked), bool(flagged), bool(evicted), bool(healed)))


SCENARIOS = [("E1", e1_deployed), ("E2", e2_isolation), ("E3", e3_e18_exclusive_and_race),
             ("E8", e8_move_to_empty_pod), ("E4", e4_fill_and_balance), ("E5", e5_full_then_early_retain),
             ("E6", e6_idle_retain_and_resume), ("E7", e7_sse_ends_and_does_not_resume), ("E9", e9_stale_gen),
             ("E24", e24_e25_tokens), ("E19", e19_gateway_restart), ("E12", e12_port_squatting),
             ("E12b", e12b_squat_during_restart), ("E26", e26_leftovers_and_e14_backoff), ("E10", e10_free_slots),
             ("E17", e17_steward_killed), ("E15", e15_pod_delete), ("E16", e16_ungraceful)]


def main() -> int:
    global CTX, TRUST, ADMIN, TOKENS
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", default="orbstack")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    CTX = a.context
    os.environ["PATH"] = "%s/.orbstack/bin:/usr/local/bin:%s" % (os.environ.get("HOME", ""), os.environ.get("PATH", ""))
    TRUST, ADMIN = secret("gateway-auth", "TRUST_SECRET"), secret("gateway-auth", "ADMIN_TOKEN")
    TOKENS = {U(n): "e2e-grmp-%s" % secrets.token_hex(8) for n in range(1, 13)}
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    log("固定池化端到端:工号前缀 %s" % RUN)
    try:
        deploy()
        for code, fn in SCENARIOS:
            if only and code not in only:
                continue
            try:
                fn()
            except Exception as exc:                # noqa: BLE001  一个场景出错不影响后面的
                record(code, fn.__name__, False, "异常:%r" % exc)
    finally:
        cleanup(a.keep)
    fails = [r for r in RESULTS if not r[2]]
    print("\n| 场景 | 内容 | 结果 | 说明 |\n|---|---|---|---|")
    for code, name, ok, detail in RESULTS:
        print("| %s | %s | %s | %s |" % (code, name, "通过" if ok else "**失败**", detail))
    print("\n共 %d 项,通过 %d,失败 %d" % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
