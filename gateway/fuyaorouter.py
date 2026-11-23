"""Fuyao 运行模式下「这个请求转到哪」(2026-11-17):每人每种环境(诊断 / 导入)一台沙箱。

接口与固定池化的 PoolRouter 相同(target / note_activity / forget;异常 NotActive / PoolFull / PoolWait;
返回 poolrouter.Target),server.py 的池化转发路径直接复用。多出来的是 `kind` 参数(导入环境也是沙箱)。

  转发   本人沙箱在跑:缓存里有目标就用;没有(网关刚重启 / 另一个副本建的)就现查,口令探活通过即可。
  恢复   本人沙箱已暂停:恢复后重新取地址、探活。平台实测恢复后地址会变而接口可能仍回旧地址 ——
         探活用的是本人的对话口令,连到别人的沙箱只会被拒,不会串数据;探不通就等,等不到回等待页。
  新建   本人没有沙箱:查开通前提 → 建沙箱(metadata 带工号与种类)→ 经 envd 下发配置并启动 → 探活。
         两个网关副本可能同时为同一人建:建完再列一次,只留最早的那台,多出来的删掉。
  满员   沙箱组没有空位:回等待页(PoolFull)。
  换版   发版换了模板(gateway-config 的 FUYAO_*_TEMPLATE):沙箱 metadata 记着建它的模板(`gsdb-template`)。
         暂停的旧版沙箱若暂停前已收尾回写(`gsdb-synced=1`)就删掉、按新模板重建;没回写成的照旧恢复,
         等它下次闲置由清扫器收尾后删除。在跑的旧版沙箱不打断,同样等闲置。

**非交互请求不建、不恢复**(探活、事件流),与固定池化同一条规矩:开着的标签页不能把暂停的沙箱拉起来。
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from . import opensandbox as osb
from . import proxy, sandboxexec, sbxtoken
from .pods import KIND_KB_IMPORT, KIND_RUNTIME
from .poolrouter import NotActive, PoolFull, PoolWait, Target

APP = "gaussdb-agent"
MD_APP, MD_USER, MD_KIND, MD_ACTIVE, MD_PAUSED = "gsdb-app", "gsdb-user", "gsdb-kind", "gsdb-active", "gsdb-paused"
MD_TEMPLATE, MD_SYNCED = "gsdb-template", "gsdb-synced"     # 建它的模板;暂停前收尾回写是否成功("1" / "0")
MD_EXPIRES = "gsdb-expires"                                  # 平台寿命的到期时刻(只在 sandbox_timeout_s > 0 时有)
# 恢复过("1"):平台 /endpoints 从此回的是暂停前的旧地址(实测),而旧地址可能已分给别人的新沙箱 ——
# 这台只认它自己用沙箱令牌向状态服务报到的地址,绝不往平台给的地址送口令、跑启动器或收尾(2026-11-18)
MD_RESUMED = "gsdb-resumed"
STALE_POLL_S = 0.5                               # 等恢复后的沙箱报到:每这么久问一次状态服务
OPENCODE_PORT, REPORTS_PORT, CLIP_PORT = 4096, 4097, 4095
KINDS = (KIND_RUNTIME, KIND_KB_IMPORT)
RELAUNCH_GAP_S = 30.0
MAX_LAUNCH_FAILS = 3                             # 同一台沙箱里启动器接连失败几次算「起不来」


def _num(text: Optional[str]) -> float:
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


class AddressUnknown(Exception):
    """恢复过的沙箱还没向状态服务报到:没有可信的地址(平台给的是旧的,可能已是别人的沙箱)。"""

    def __init__(self, sandbox_id: str):
        super().__init__("沙箱 %s 恢复后还没向状态服务报到,地址未知" % sandbox_id)
        self.sandbox_id = sandbox_id


class LaunchError(Exception):
    """启动器在沙箱里失败了(退出码非 0)。"""


class SandboxBroken(Exception):
    """同一台沙箱里启动器接连失败:不再让用户对着「正在准备」干等,回「环境准备失败」并记日志。
    不自动重建 —— 模板本身坏了重建只会循环;运维看日志后经 /admin/fuyao 删除,下次登录重建。"""


@dataclass(frozen=True)
class Policy:
    templates: Mapping[str, str]                 # 种类 → E2B 模板名
    groups: Mapping[str, str]                    # 种类 → 沙箱组
    cpu: str = "2"
    memory: str = "4Gi"
    sandbox_timeout_s: int = 0                   # 0 = 不给平台寿命(实测:不带 timeout 就没有到期时间)。
    # >0:平台到点直接删、不能续期、虚拟机收不到信号(实测,上限 24 小时)—— 清扫器在到期前 expire_margin_s 收尾后删除
    expire_margin_s: int = 900
    idle_s: int = 1800                           # 闲置多久收尾并暂停(或删除)
    retain_s: int = 7 * 86400                    # 暂停多久后删除
    pause: bool = True                           # False:闲置即收尾后删除(平台不支持暂停时)
    wait_s: float = 90.0                         # 一次请求最多等多久;等不到回等待页
    retry_after: int = 30
    launch_argv: Tuple[str, ...] = ("python3", "/opt/agent/podctl.py", "fuyao-start")
    stop_argv: Tuple[str, ...] = ("python3", "/opt/agent/podctl.py", "fuyao-stop")
    launch_timeout_s: float = 150.0
    stop_timeout_s: float = 150.0


def probe(host: str, password: str, port: int = OPENCODE_PORT, timeout: float = 3.0) -> bool:
    """opencode 就绪且口令对得上(连到别人的沙箱会 401)。"""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/global/health", headers={"Authorization": proxy.basic_auth(password)})
        r = conn.getresponse()
        r.read()
        return r.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def busy(host: str, password: str, port: int = OPENCODE_PORT, timeout: float = 5.0) -> bool:
    """有会话在跑(opencode 1.18.27:GET /session/status,type 为 busy / retry)。连不上按不忙算。"""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/session/status", headers={"Authorization": proxy.basic_auth(password)})
        r = conn.getresponse()
        data = r.read()
        if r.status != 200:
            return False
        st = json.loads(data or b"{}")
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        conn.close()
    return isinstance(st, dict) and any(isinstance(v, dict) and v.get("type") in ("busy", "retry") for v in st.values())


def owner(s: osb.Sandbox) -> Tuple[str, str]:
    return s.metadata.get(MD_USER, ""), s.metadata.get(MD_KIND, "")


def outdated(s: osb.Sandbox, policy: Policy) -> bool:
    """不是按当前模板建的(发版换了模板;没记模板的老沙箱也算)。"""
    want = policy.templates.get(owner(s)[1], "")
    return bool(want) and s.metadata.get(MD_TEMPLATE, "") != want


def synced_before_pause(s: osb.Sandbox) -> bool:
    return s.metadata.get(MD_SYNCED) == "1"


def expires_at(s: osb.Sandbox) -> float:
    """平台寿命的到期时刻;0 = 没有。列举接口不回 expiresAt(实测),建沙箱时记进 metadata。"""
    try:
        return s.expires_at or float(s.metadata.get(MD_EXPIRES) or 0)
    except ValueError:
        return 0.0


class FuyaoRouter:
    handles_kb_import = True                     # server.py 据此把导入环境也交给本路由器

    def __init__(self, client: osb.Client, envd: sandboxexec.Envd, policy: Policy, key: bytes,
                 password_for: Callable[[str], str], launch_env: Callable[[str, str], Mapping[str, str]],
                 prereq: Optional[Callable[[str, str], None]] = None,
                 prober: Callable[[str, str], bool] = probe, log: Callable[[str], None] = print,
                 locate: Optional[Callable[[str, str, str], Optional[str]]] = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        if len(key) < 16:
            raise ValueError("控制密钥至少 16 字节")
        self.os, self.envd, self.policy, self.key = client, envd, policy, key
        self.password_for, self.launch_env, self.prereq, self.prober = password_for, launch_env, prereq, prober
        self.log, self.clock, self.sleep, self.locate = log, clock, sleep, locate
        self._creds: Dict[Tuple[str, str], Target] = {}
        self._locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._launched: Dict[str, float] = {}
        self._fails: Dict[str, int] = {}             # 沙箱 → 启动器接连失败次数(连不上 envd 不算)
        self._activity: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    # -- 活动(交互请求才记;回收器定期写进沙箱 metadata) -------------------------------------------

    def note_activity(self, user: str, kind: str = KIND_RUNTIME) -> None:
        with self._lock:
            self._activity[(user, kind)] = self.clock()

    def drain_activity(self) -> Dict[Tuple[str, str], float]:
        with self._lock:
            out, self._activity = self._activity, {}
        return out

    def restore_activity(self, acts: Mapping[Tuple[str, str], float]) -> None:
        with self._lock:
            for k, ts in acts.items():
                if ts > self._activity.get(k, 0.0):
                    self._activity[k] = ts

    def forget(self, user: str, kind: Optional[str] = None) -> None:
        """缓存的目标作废(转发失败 / 沙箱被暂停或删除),下次重新查。"""
        with self._lock:
            for k in [k for k in self._creds if k[0] == user and (kind is None or k[1] == kind)]:
                del self._creds[k]

    def forget_sandbox(self, sandbox_id: str) -> None:
        with self._lock:
            for k in [k for k, t in self._creds.items() if t.pod == sandbox_id]:
                del self._creds[k]
            self._launched.pop(sandbox_id, None)
            self._fails.pop(sandbox_id, None)

    def cached(self) -> Dict[Tuple[str, str], Target]:
        with self._lock:
            return dict(self._creds)

    # -- 主入口 -------------------------------------------------------------------------------------

    def target(self, user: str, interactive: bool = True, kind: str = KIND_RUNTIME) -> Target:
        if kind not in KINDS:
            raise ValueError("不认识的环境种类 %r" % kind)
        key = (user, kind)
        with self._lock:
            got = self._creds.get(key)
            lock = self._locks.setdefault(key, threading.Lock())
        if got is not None:
            return got
        with lock:
            with self._lock:
                got = self._creds.get(key)
            if got is not None:
                return got
            deadline = self.clock() + self.policy.wait_s
            while True:
                got, why = self._attempt(user, kind, interactive)
                if got is not None:
                    with self._lock:
                        self._creds[key] = got
                    return got
                if self.clock() >= deadline:
                    raise PoolWait(why, min(self.policy.retry_after, 10))
                self.sleep(STALE_POLL_S if why == "address_stale" else 1.0)

    # -- 各条路 -------------------------------------------------------------------------------------

    def mine(self, user: str, kind: str) -> List[osb.Sandbox]:
        """本人这一种环境的沙箱,最早建的在前(两个副本并发建出两台时,大家都认最早那台)。"""
        boxes = self.os.list({MD_APP: APP, MD_USER: user, MD_KIND: kind})
        return sorted((b for b in boxes if b.running or b.paused or b.state in ("Pending", "Creating", "")),
                      key=lambda b: (b.created_at, b.id))

    def _attempt(self, user: str, kind: str, interactive: bool) -> Tuple[Optional[Target], str]:
        try:
            boxes = self.mine(user, kind)
        except osb.OpenSandboxError as exc:
            self.log("Fuyao:列举 %s 的沙箱失败:%s" % (user, exc))
            return None, "platform_unreachable"
        if boxes and boxes[0].paused and outdated(boxes[0], self.policy) and synced_before_pause(boxes[0]):
            if not interactive:
                raise NotActive(user)
            self._drop(boxes[0].id, "模板已更新,暂停前已收尾回写;按新模板重建")
            boxes = boxes[1:]
        if not boxes:
            if not interactive:
                raise NotActive(user)
            if self.prereq is not None:
                self.prereq(user, kind)              # PrerequisiteMissing 原样抛给 server.py
            created = self._create(user, kind)
            try:
                boxes = self.mine(user, kind)
            except osb.OpenSandboxError:
                boxes = [created]
            keeper = boxes[0] if boxes else created
            if keeper.id != created.id:              # 另一个副本先建好了:用它的,把自己这台删掉
                self._drop(created.id, "并发重复")
            return self._ready(keeper, user, kind, launch=keeper.id == created.id)
        keeper, extra = boxes[0], boxes[1:]
        for b in extra:
            self._drop(b.id, "同一人同一种环境多出来的")
        if keeper.paused:
            if not interactive:
                raise NotActive(user)
            try:
                at = self.resume(keeper.id)
            except osb.OpenSandboxError as exc:
                self.log("Fuyao:恢复 %s 的沙箱 %s 失败:%s" % (user, keeper.id, exc))
                return None, "resume_failed"
            self.log("Fuyao:%s 的%s沙箱 %s 已恢复" % (user, _kind_name(kind), keeper.id))
            return self._ready(keeper, user, kind, launch=False, resumed_at=at)
        if keeper.running:
            return self._ready(keeper, user, kind, launch=False)
        return None, "starting:%s" % (keeper.state or "unknown")

    def resume(self, sandbox_id: str) -> float:
        """恢复一台暂停的沙箱,返回恢复时刻。**先**写恢复标记再恢复:标记写不进就不恢复 —— 否则恢复后的沙箱
        不带标记,别的副本 / 清扫器会退回平台给的旧地址(可能已是别人的沙箱)。"""
        at = self.clock()
        self.os.patch_metadata(sandbox_id, {MD_RESUMED: str(int(at))})
        self.os.resume(sandbox_id)
        self.os.patch_metadata(sandbox_id, {MD_PAUSED: None, MD_SYNCED: None, MD_ACTIVE: str(int(at))})
        return float(int(at))

    def _create(self, user: str, kind: str) -> osb.Sandbox:
        p = self.policy
        now = self.clock()
        md = {MD_APP: APP, MD_USER: user, MD_KIND: kind, MD_ACTIVE: str(int(now)), MD_TEMPLATE: p.templates[kind]}
        if p.sandbox_timeout_s > 0:          # 建之前取时刻:比平台算的早一点,只会提前收尾
            md[MD_EXPIRES] = str(int(now + p.sandbox_timeout_s))
        try:
            s = self.os.create(p.templates[kind], p.groups[kind], md, p.cpu, p.memory, p.sandbox_timeout_s)
        except osb.CapacityError:
            raise PoolFull(p.retry_after)
        except osb.OpenSandboxError as exc:
            if 500 <= exc.status < 600:
                # 平台满员时,OpenSandbox 把调度器的 503「no available agent」包成 500(鲲鹏 2026-11-18 实测,平台缺陷):
                # 5xx 一律按「平台暂时没有空位」让用户等、自动重试,平台原话记日志;4xx 是请求本身不对,照旧当错误
                self.log("Fuyao:为 %s 建%s沙箱时平台回 %d,按暂时没有空位处理、让用户等待重试(平台原话:%s)"
                         % (user, _kind_name(kind), exc.status, str(exc)[:300]))
                raise PoolWait("platform_busy", p.retry_after)
            raise
        self.log("Fuyao:为 %s 建%s沙箱 %s(模板 %s,沙箱组 %s)" % (user, _kind_name(kind), s.id, p.templates[kind],
                                                         p.groups[kind]))
        return s

    def _drop(self, sandbox_id: str, why: str) -> None:
        try:
            self.os.delete(sandbox_id)
            self.log("Fuyao:删除沙箱 %s(%s)" % (sandbox_id, why))
        except osb.OpenSandboxError as exc:
            self.log("Fuyao:删除沙箱 %s(%s)失败,交给回收器:%s" % (sandbox_id, why, exc))
        self.forget_sandbox(sandbox_id)

    def address(self, s: osb.Sandbox, resumed_at: Optional[float] = None) -> str:
        """沙箱现在的地址:优先用状态服务最近看到的来源地址(这台沙箱用自己的令牌报到,地址可信)。
        恢复过的(resumed_at,或 metadata 的恢复时刻)只认**恢复之后**报上来的,问不到就抛 AddressUnknown:
        平台 /endpoints 恢复后回的是旧地址(实测),暂停前的报到记录也是旧地址,而旧地址可能已分给别人的沙箱 ——
        往那里探活会送出本人口令,跑启动器 / 收尾会把本人令牌写进别人的沙箱。
        没恢复过的才退回平台给的地址(新建时平台给的就是它的)。"""
        user, kind = owner(s)
        if resumed_at is None:
            resumed_at = _num(s.metadata.get(MD_RESUMED)) or None
        if self.locate is not None and user:
            try:
                got = self.locate(s.id, user, kind)
            except Exception as exc:                    # noqa: BLE001 —— 状态服务问不到:没恢复过的退回平台给的地址
                self.log("Fuyao:向状态服务问沙箱 %s 的地址失败:%s" % (s.id, exc))
                got = None
            if got:
                addr, age = got
                if resumed_at is None or age <= max(self.clock() - resumed_at, 0.0):
                    return addr
        if resumed_at is not None:
            raise AddressUnknown(s.id)
        return self.os.address(s.id)

    def _ready(self, s: osb.Sandbox, user: str, kind: str, launch: bool,
               resumed_at: Optional[float] = None) -> Tuple[Optional[Target], str]:
        resumed = resumed_at is not None
        try:
            host = self.address(s, resumed_at=resumed_at)
        except AddressUnknown:
            return None, "address_stale"
        except osb.OpenSandboxError as exc:
            self.log("Fuyao:取 %s 的地址失败:%s" % (s.id, exc))
            return None, "no_address"
        password = self.password_for(user)
        if launch:
            try:
                self.launch(s.id, host, user, kind, password)
            except (LaunchError, sandboxexec.ExecError) as exc:
                self.log("Fuyao:%s 的%s沙箱 %s 启动失败:%s" % (user, _kind_name(kind), s.id, exc))
                self._failed(s.id, exc)
                return None, "launch_failed"
        if self.prober(host, password):
            return self._ok(s.id, host, password), "ready"
        # 没就绪:另一个副本还在启动它 / 上次启动半途而废(网关重启)/ 恢复后地址还没对上。
        # envd 在、隔一阵没起过 → 再起一次(启动器幂等)。
        now = self.clock()
        if not resumed and now - self._launched.get(s.id, 0.0) >= RELAUNCH_GAP_S and self.envd.healthy(host):
            try:
                self.launch(s.id, host, user, kind, password)
            except (LaunchError, sandboxexec.ExecError) as exc:
                self.log("Fuyao:%s 的沙箱 %s 补起失败:%s" % (user, s.id, exc))
                self._failed(s.id, exc)
                return None, "launch_failed"
            if self.prober(host, password):
                return self._ok(s.id, host, password), "ready"
        return None, "address_stale" if resumed else "starting"

    def _ok(self, sandbox_id: str, host: str, password: str) -> Target:
        with self._lock:
            self._fails.pop(sandbox_id, None)
        return self._target(sandbox_id, host, password)

    def _failed(self, sandbox_id: str, exc: Exception) -> None:
        if not isinstance(exc, LaunchError):
            return                                   # envd 连不上:可能是网络抖动,不算沙箱坏
        with self._lock:
            n = self._fails[sandbox_id] = self._fails.get(sandbox_id, 0) + 1
        if n >= MAX_LAUNCH_FAILS:
            raise SandboxBroken("沙箱 %s 启动器接连失败 %d 次(最近一次:%s);可在 /admin/fuyao 删除后重建"
                                % (sandbox_id, n, exc))

    def launch(self, sandbox_id: str, host: str, user: str, kind: str, password: str) -> None:
        env = sandboxexec.env_from(self.launch_env(user, kind))
        env.update({
            "GSDB_USER_ID": user,
            "GSDB_KIND": kind,
            "GSDB_SANDBOX_ID": sandbox_id,
            "GSDB_SANDBOX_TOKEN": sbxtoken.issue(self.key, sandbox_id, user, kind, now=self.clock()),
            "OPENCODE_SERVER_PASSWORD": password,
            "GSDB_REPORTS_TOKEN": sbxtoken.reports_token(self.key, sandbox_id),
        })
        with self._lock:
            self._launched[sandbox_id] = self.clock()
        res = self.envd.run(host, list(self.policy.launch_argv), user="root", envs=env,
                            timeout_s=self.policy.launch_timeout_s)
        if not res.ok:
            tail = (res.stderr or res.stdout).strip().splitlines()[-3:]
            raise LaunchError("退出码 %d:%s" % (res.exit_code, " / ".join(tail)[:400]))

    def stop(self, sandbox_id: str, host: str, release: bool = False) -> bool:
        """收尾同步(删除 / 暂停之前)。release:同时放开本人目录的独占(删除之前;暂停时不放,回来还是这台)。"""
        argv = list(self.policy.stop_argv) + (["--release"] if release else [])
        try:
            res = self.envd.run(host, argv, user="root", timeout_s=self.policy.stop_timeout_s)
        except sandboxexec.ExecError as exc:
            self.log("Fuyao:沙箱 %s 收尾同步时连不上:%s" % (sandbox_id, exc))
            return False
        if not res.ok:
            tail = (res.stderr or res.stdout).strip().splitlines()[-2:]
            self.log("Fuyao:沙箱 %s 收尾同步失败(退出码 %d):%s" % (sandbox_id, res.exit_code, " / ".join(tail)[:300]))
        return res.ok

    def _target(self, sandbox_id: str, host: str, password: str) -> Target:
        return Target(pod=sandbox_id, slot=0, host=host, port=OPENCODE_PORT, password=password,
                      reports_port=REPORTS_PORT, reports_token=sbxtoken.reports_token(self.key, sandbox_id),
                      since=self.clock())


def _kind_name(kind: str) -> str:
    return "导入" if kind == KIND_KB_IMPORT else "诊断"
