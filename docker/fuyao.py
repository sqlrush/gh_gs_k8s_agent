"""沙箱里的启动器(Fuyao 运行模式,2026-11-17):网关经 envd 以 root 调用。

`podctl fuyao-start`(配置经 envd 的进程环境变量传入;创建沙箱时给的 env 不进虚拟机,平台实测):
  1. 已在运行且口令探活通过 → 直接返回(幂等,网关可以重试);
  2. 删掉模板里撑磁盘用的占位文件(平台按镜像大小定磁盘,见设计文档 §2);
  3. 沙箱令牌写进只有 root 可读的文件;跑 opencode 的 agent 用户读不到它;
  4. 向状态服务取得本人目录的独占(上一台沙箱的标记要过期才能接管,最多等 60 秒)→ 载入状态 → 拉知识库;
  5. 用虚拟机里的 systemd 起三个服务(envd 会等子进程结束,后台服务不能用 &):
     gsdb-credrelay(root)  credproxy.sock + 模型端口,加令牌转给集群凭据代理服务;
     gsdb-statesync(root)  每 30 秒续独占、每 60 秒回写、诊断环境拉知识库 / 导入环境发布知识库;
     gsdb-agent(agent)     entrypoint.sh(FUYAO=1):opencode、报告端口(带报告令牌)、导入环境的剪藏接收;
  6. 等 opencode 用本人口令探活通过才返回(网关据此认为就绪)。
`podctl fuyao-stop [--release]`(网关在暂停 / 删除之前调用;删除时带 --release 放开独占):
  导入环境先发布知识库 → 以 agent 身份备份对话库 → 完整回写并清 dirty 标记。
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Mapping, Optional

import remotesync as rs
import sbxtoken

DATA = "/data"
ME, KB = DATA + "/me", DATA + "/kb"                 # 按需模式的 $NAS_ME / $NAS_KB 在沙箱里的替身(本地盘)
RUN = "/run/gsdb"
TOKEN_FILE, ROOT_ENV, AGENT_ENV = RUN + "/sandbox-token", RUN + "/root.env", RUN + "/agent.env"
VAR = "/var/lib/gsdb"
KB_STATE, RESERVE = VAR + "/kb-state.json", VAR + "/disk-reserve"
UNIT_AGENT, UNIT_RELAY, UNIT_SYNC = "gsdb-agent", "gsdb-credrelay", "gsdb-statesync"
PODCTL = ["python3", "-u", "/opt/agent/podctl.py"]
OPENCODE_PORT = 4096
LEASE_WAIT_S, READY_WAIT_S = 60, 90
SYNC_S, LEASE_S, KB_S = 60, 30, 60
# 给 agent 的环境变量:agent-config 的键(GRMP_*、MODEL_*、KB_*)与网关给的 GSDB_* / OPENCODE_*;
# 沙箱令牌与两个集群服务的地址只给 root(agent 用不着,也不该拿到)
_AGENT_PREFIXES = ("GRMP_", "MODEL_", "KB_", "GSDB_", "OPENCODE_")
_ROOT_ONLY = ("GSDB_SANDBOX_TOKEN", "GSDB_STATE_URL", "GSDB_CREDPROXY_URL")
# 镜像里 Dockerfile 的 ENV 在 systemd 起的服务里不生效,这里补上
_IMAGE_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "TZ": "Asia/Shanghai", "LANG": "C.UTF-8",
              "GSDB_ROOTSPEC_HOME": "/workspace"}


def log(msg: str) -> None:
    print("启动器:%s" % msg, flush=True)


def _ids(env: Mapping[str, str]):
    return int(env.get("GSDB_AGENT_UID") or 1000), int(env.get("GSDB_AGENT_GID") or 1000)


def _as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _write(path: str, text: str, mode: int, gid: Optional[int] = None) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fchmod(fd, mode)
        if gid is not None and _as_root():
            os.fchown(fd, 0, gid)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# 多行值:systemd 的环境文件一行一个变量。这些键本身认「;」分隔(podctl.parse_instance_aliases 先按行、再按「;」切),
# 合成一行语义不变;其余多行值写不进去,记日志说清楚(不悄悄丢)
_JOIN_LINES = ("GRMP_INSTANCE_ALIASES",)


def env_file(mapping: Mapping[str, str]) -> str:
    """systemd EnvironmentFile 格式:KEY="值",反斜杠与双引号转义。"""
    lines, dropped = [], []
    for k in sorted(mapping):
        v = str(mapping[k])
        if k in _JOIN_LINES:
            v = "; ".join(s.strip() for s in v.splitlines() if s.strip())
        if not k.replace("_", "").isalnum():
            dropped.append(k)
            continue
        if "\n" in v or "\r" in v:
            dropped.append(k)
            continue
        lines.append('%s="%s"' % (k, v.replace("\\", "\\\\").replace('"', '\\"')))
    if dropped:
        log("这些变量是多行值或名字不合法,写不进 systemd 环境文件,没有下发:%s" % "、".join(dropped))
    return "\n".join(lines) + "\n"


def read_env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        k, _s, v = line.partition("=")
        if k and v.startswith('"') and v.endswith('"'):
            out[k] = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return out


def agent_env(env: Mapping[str, str], kind: str) -> Dict[str, str]:
    out = dict(_IMAGE_ENV)
    out.update({k: v for k, v in env.items() if k.startswith(_AGENT_PREFIXES) and k not in _ROOT_ONLY})
    out.update({
        "FUYAO": "1", "NAS_ME": ME, "NAS_KB": KB, "STATE_LOCAL": ME, "HOME": DATA + "/home",
        "POD_NAME": env.get("GSDB_SANDBOX_ID", ""),
        "MODEL_PROXY_URL": "http://127.0.0.1:4099/model",          # 与按需模式的 Pod 模板相同
        "GRMP_PROXY_SOCKET": "/run/credproxy/credproxy.sock",
        "REPORTS_TOKEN": env.get("GSDB_REPORTS_TOKEN", ""),
    })
    if kind == "kb-import":
        out.update({"KB_CLIP_RECEIVER": "1", "AGENT_ROLE": "kb-import", "GSDB_KB_INBOX": KB + "/inbox/uploads"})
    out.pop("MODEL_API_KEY", None)
    out.pop("GRMP_AUTH_TOKEN", None)
    return out


def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def run_unit(name: str, argv: List[str], env_path: Optional[str] = None, uid: Optional[int] = None,
             gid: Optional[int] = None, restart: str = "on-failure", workdir: str = "/data") -> None:
    """起一个 systemd 服务。同名的先停掉并清掉失败记录(重试启动器时不报「unit 已存在」)。"""
    _systemctl("stop", name)
    _systemctl("reset-failed", name)
    cmd = ["systemd-run", "--unit", name, "--collect", "--property", "Restart=%s" % restart,
           "--property", "RestartSec=3", "--property", "WorkingDirectory=%s" % workdir]
    if env_path:
        cmd += ["--property", "EnvironmentFile=%s" % env_path]
    if uid is not None:
        cmd += ["--uid", str(uid), "--gid", str(gid if gid is not None else uid)]
    subprocess.run(cmd + argv, check=True, stdout=subprocess.DEVNULL)


def active(name: str) -> bool:
    return _systemctl("is-active", "--quiet", name) == 0


def healthy(password: str, timeout: float = 3.0) -> bool:
    req = urllib.request.Request("http://127.0.0.1:%d/global/health" % OPENCODE_PORT, headers={
        "Authorization": "Basic " + base64.b64encode(("opencode:%s" % password).encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _journal(name: str, n: int = 8) -> str:
    try:
        out = subprocess.run(["journalctl", "-u", name, "-n", str(n), "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return " / ".join(l.strip() for l in out.splitlines() if l.strip())[-600:]


# -- 启动 ---------------------------------------------------------------------------------------------

def _owner_sandbox() -> Optional[str]:
    """这台虚拟机已经属于哪台沙箱(第一次启动时写下的令牌);还没启动过返回 None。"""
    try:
        text = pathlib.Path(TOKEN_FILE).read_text().strip()
    except FileNotFoundError:
        return None
    return sbxtoken.sandbox_of(text) or "(令牌损坏)"


def start(env: Mapping[str, str] = os.environ) -> int:
    need = [k for k in ("GSDB_USER_ID", "GSDB_KIND", "GSDB_SANDBOX_TOKEN", "GSDB_STATE_URL", "GSDB_CREDPROXY_URL",
                        "OPENCODE_SERVER_PASSWORD") if not env.get(k)]
    if need:
        log("缺环境变量:%s(网关经 envd 下发)" % "、".join(need))
        return 2
    mine = _owner_sandbox()
    asked = sbxtoken.sandbox_of(env["GSDB_SANDBOX_TOKEN"])
    if mine is not None and mine != asked:
        # 第二道防线:网关认错了地址(恢复后平台回旧地址、旧地址已分给别人),不能把别人的令牌和口令写进来
        log("这台虚拟机属于沙箱 %s,拒绝以沙箱 %s 的身份启动(网关把地址认错了),什么都不改" % (mine, asked))
        return 6
    kind, password = env["GSDB_KIND"], env["OPENCODE_SERVER_PASSWORD"]
    uid, gid = _ids(env)
    if active(UNIT_AGENT) and healthy(password):
        log("已在运行,不重复启动")
        return 0
    try:
        os.unlink(RESERVE)
        log("已删除磁盘占位文件,空出空间给用户状态")
    except FileNotFoundError:
        pass
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    os.makedirs(VAR, mode=0o700, exist_ok=True)
    _write(TOKEN_FILE, env["GSDB_SANDBOX_TOKEN"], 0o600)
    _write(ROOT_ENV, env_file({"GSDB_STATE_URL": env["GSDB_STATE_URL"], "GSDB_CREDPROXY_URL": env["GSDB_CREDPROXY_URL"],
                               "GSDB_KIND": kind, "GSDB_SANDBOX_TOKEN_FILE": TOKEN_FILE,
                               "GSDB_AGENT_UID": str(uid), "GSDB_AGENT_GID": str(gid)}), 0o600)
    client = rs.Client(env["GSDB_STATE_URL"], env["GSDB_SANDBOX_TOKEN"])
    deadline = time.time() + LEASE_WAIT_S
    while True:
        try:
            got = rs.lease(client, "acquire")
            if got.get("took_over"):
                log("接管了上一台沙箱的过期独占(%s)" % got["took_over"])
            break
        except rs.LeaseLost as exc:
            if time.time() >= deadline:
                log("本人目录仍被另一台沙箱持有,不启动:%s" % exc)
                return 3
            time.sleep(5)
        except rs.StateError as exc:
            log("连不上状态服务:%s" % exc)
            return 4
    try:
        if not os.path.exists(os.path.join(ME, rs.statesync.MARK_LOADED)):
            n = rs.load(client, ME, owner=(uid, gid))
            log("已载入本人状态(%d 个文件)" % n)
        kb = rs.kb_pull(client, KB, KB_STATE, writable_by=(uid, gid) if kind == "kb-import" else None)
        log("知识库 v%s(新取 %d、删除 %d)" % (kb["version"], kb["fetched"], kb["removed"]))
    except rs.StateError as exc:
        log("载入失败:%s" % exc)
        return 4
    for name in ("home", "config", "cache", "oc"):
        d = os.path.join(DATA, name)
        os.makedirs(d, exist_ok=True)
        if _as_root():
            os.chown(d, uid, gid)
    _write(AGENT_ENV, env_file(agent_env(env, kind)), 0o640, gid=gid)
    run_unit(UNIT_RELAY, PODCTL + ["serve-credrelay"], env_path=ROOT_ENV, restart="always")
    run_unit(UNIT_SYNC, PODCTL + ["fuyao-syncloop"], env_path=ROOT_ENV, restart="on-failure")
    run_unit(UNIT_AGENT, ["/opt/agent/entrypoint.sh"], env_path=AGENT_ENV, uid=uid, gid=gid, workdir=DATA)
    deadline = time.time() + READY_WAIT_S
    while time.time() < deadline:
        if healthy(password):
            log("就绪(%s)" % kind)
            return 0
        if not active(UNIT_AGENT) and _systemctl("is-failed", "--quiet", UNIT_AGENT) == 0:
            break
        time.sleep(1)
    log("opencode 没有就绪:%s" % _journal(UNIT_AGENT))
    return 5


# -- 后台同步 -----------------------------------------------------------------------------------------

def syncloop(env: Mapping[str, str] = os.environ, once: bool = False) -> int:
    token = pathlib.Path(env.get("GSDB_SANDBOX_TOKEN_FILE") or TOKEN_FILE).read_text().strip()
    client = rs.Client(env["GSDB_STATE_URL"], token)
    kind = env.get("GSDB_KIND", "runtime")
    uid, gid = _ids(env)
    last = {"lease": 0.0, "sync": 0.0, "kb": 0.0}
    while True:
        now = time.time()
        try:
            if now - last["lease"] >= LEASE_S:
                rs.lease(client, "refresh")
                last["lease"] = now
            else:
                # 每一拍(5 秒)报到一次:平台恢复暂停的沙箱后不回新地址,网关从状态服务取 —— 报得勤,恢复后几秒就能连上
                rs.hello(client)
            if now - last["sync"] >= SYNC_S:
                out = rs.sync(client, ME)
                last["sync"] = now
                if not out.get("skipped"):
                    log("回写:%s" % json.dumps({k: out.get(k) for k in ("uploaded", "deleted", "errors")},
                                              ensure_ascii=False))
            if now - last["kb"] >= KB_S:
                last["kb"] = now
                if kind == "kb-import":
                    out = rs.kb_publish(client, KB, KB_STATE)
                    if out.get("stale"):
                        log("知识库发布被拒:另一位管理员先发布了 v%s,请先刷新再导入" % out.get("server_version"))
                    elif out.get("version"):
                        log("知识库已发布 v%s(写入 %d、删除 %d)" % (out["version"], out["published"], out["removed"]))
                else:
                    rs.kb_pull(client, KB, KB_STATE)
        except rs.LeaseLost as exc:
            log("独占已不在本沙箱手里,停止回写(不能拿旧副本盖掉别人的):%s" % exc)
            return 0
        except rs.StateError as exc:
            log("这一轮同步失败,稍后重试:%s" % exc)
        if once:
            return 0
        time.sleep(5)


# -- 收尾 ---------------------------------------------------------------------------------------------

def stop(release: bool = False) -> int:
    env = read_env_file(ROOT_ENV)
    if not env.get("GSDB_STATE_URL"):
        log("沙箱还没启动过(没有 %s),不用收尾" % ROOT_ENV)
        return 0
    client = rs.Client(env["GSDB_STATE_URL"], pathlib.Path(TOKEN_FILE).read_text().strip())
    uid = str(_ids(env)[0])
    try:
        if env.get("GSDB_KIND") == "kb-import":
            out = rs.kb_publish(client, KB, KB_STATE)
            log("收尾前发布知识库:%s" % json.dumps(out, ensure_ascii=False))
        db = os.path.join(ME, "xdg-data", "opencode", "opencode.db")
        if os.path.exists(db):
            try:                                     # 以 agent 身份备份(root 来做会留下 agent 动不了的文件)
                subprocess.run(["runuser", "-u", _user(uid), "--"] + PODCTL +
                               ["db-backup", "--db", db, "--backup-dir", os.path.join(ME, "backup"), "--keep", "3"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            except (OSError, subprocess.SubprocessError) as exc:
                log("对话库备份没做成(不影响收尾同步):%s" % exc)
        out = rs.finish(client, ME, release=release)
        log("收尾同步完成:%s" % json.dumps({k: out.get(k) for k in ("uploaded", "deleted", "finished")},
                                          ensure_ascii=False))
        return 0
    except rs.LeaseLost as exc:
        log("收尾时独占已不在本沙箱手里,不回写:%s" % exc)
        return 3
    except rs.StateError as exc:
        log("收尾同步失败:%s" % exc)
        return 1


def _user(uid: str) -> str:
    import pwd
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except KeyError:
        return "agent"


if __name__ == "__main__":
    sys.exit(start())
