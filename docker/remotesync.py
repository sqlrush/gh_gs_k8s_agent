"""沙箱这一侧的状态同步(Fuyao 运行模式,2026-11-17):本地盘 ⇄ 集群里的状态服务(docker/statesvc.py)⇄ NAS。

沙箱挂不了 NAS(平台实测),所以按需模式里 statesync「本地 ⇄ NAS」的那一步,在这里变成「本地 ⇄ 状态服务」:
- load:   取本人状态解到本地(本地目录 = 按需模式的 $NAS_ME 的替身,里面就是 SYNC_DIRS 这几个子目录),
          属主交还给 agent;记下当前状态作基线,第一次回写不会把刚载入的东西原样传回去;
- sync:   算出改动(普通文件比大小与修改时间;库沿用 statesync 的签名判据,`-wal` 非空一律算改动),
          没改动也没删除就**不发请求**;有就打包上传(普通文件逐级不跟随链接地读;库先 online backup 出一致性快照)
          并附上「现有文件清单」,状态服务据此传播删除(只在载入成功过之后);
- finish: 完整同步一次再清 dirty 标记(删除 / 暂停之前由网关经 envd 调用)。
独占被别人接管(状态服务回 409 not_owner / lost)时抛 LeaseLost,调用方停止回写 —— 不能拿旧副本盖掉别人的。

知识库:诊断沙箱按清单增量拉取只读副本(官方文档集打在模板里,不拉);导入沙箱在本地导入,
库没有被锁住(agent/common/kb/lock.py 的 .lock)时把改动整体发布,别人先发布了就回 stale、不覆盖。

本模块以 root 运行(沙箱令牌只给 root 可读),读的是 agent 写的文件 —— 一律经 fdtree 不跟随链接,
模型埋一个指向令牌文件的链接也带不走。
"""
from __future__ import annotations

import http.client
import json
import os
import pathlib
import shutil
import tarfile
import tempfile
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

import fdtree
import statesync
import treetar

SYNC_DIRS = tuple(statesync.LOCALIZED) + ("gdaa", "reports", "backup")      # 与 statesvc.SYNC_DIRS 一致
RECORD = ".remote-sync.json"
TMP_DIR = ".remote-sync-tmp"
KB_LOCK, KB_LOCK_STALE_S = ".lock", 600
KB_DOCSETS = "docsets"
HDR_KB_BASE = "X-Gsdb-Kb-Base"
MAX_FILE = 4 << 30
PATHS_PER_REQUEST = 2000


class StateError(Exception):
    pass


class LeaseLost(StateError):
    """本人目录的独占已不在这台沙箱手里。"""


def _is_db(rel: str) -> bool:
    return rel.lower().endswith(statesync._DB_SUFFIXES)


def _skip_journal(rel: str) -> bool:
    return rel.rpartition("/")[2].endswith(statesync._SKIP_SUFFIXES)


class Client:
    def __init__(self, base_url: str, token: str, timeout_s: float = 120.0):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme != "http" or not u.hostname:
            raise ValueError("状态服务地址要写成 http://主机:端口")
        self.host, self.port, self.timeout_s = u.hostname, u.port or 80, timeout_s
        self.token = token

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Authorization": "Bearer " + self.token}
        h.update(extra or {})
        return h

    def _open(self, method: str, path: str, body=None, headers=None, length: Optional[int] = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        h = self._headers(headers)
        if length is not None:
            h["Content-Length"] = str(length)
        try:
            conn.request(method, path, body=body, headers=h)
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise StateError("连不上状态服务(%s:%d):%s" % (self.host, self.port, exc)) from exc

    def call_json(self, method: str, path: str, obj: Optional[dict] = None) -> Tuple[int, dict]:
        data = json.dumps(obj or {}).encode("utf-8")
        conn, resp = self._open(method, path, body=data, headers={"Content-Type": "application/json"}, length=len(data))
        try:
            raw = resp.read()
            try:
                out = json.loads(raw or b"{}")
            except ValueError:
                out = {"error": raw[:200].decode("utf-8", "replace")}
            return resp.status, out if isinstance(out, dict) else {}
        finally:
            conn.close()

    def get_json(self, path: str) -> Tuple[int, dict]:
        conn, resp = self._open("GET", path)
        try:
            raw = resp.read()
            return resp.status, (json.loads(raw or b"{}") if resp.status == 200 else {"error": raw[:200].decode()})
        finally:
            conn.close()

    def upload(self, method: str, path: str, file_path: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, dict]:
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            conn, resp = self._open(method, path, body=f, headers=dict(headers or {}, **{"Content-Type": "application/x-tar"}),
                                    length=size)
        try:
            raw = resp.read()
            try:
                out = json.loads(raw or b"{}")
            except ValueError:
                out = {"error": raw[:200].decode("utf-8", "replace")}
            return resp.status, out if isinstance(out, dict) else {}
        finally:
            conn.close()


# -- 独占 ---------------------------------------------------------------------------------------------

def hello(client: Client) -> None:
    """报到:状态服务记下本沙箱现在的来源地址(不碰 NAS)。失败不要紧,下一拍再报。"""
    code, out = client.call_json("POST", "/v1/hello", {})
    if code != 200:
        raise StateError("状态服务 hello 回了 %d:%s" % (code, out.get("error")))


def lease(client: Client, action: str) -> dict:
    code, out = client.call_json("POST", "/v1/lease", {"action": action})
    if code == 409:
        raise LeaseLost("本人目录的独占在 %s 手里(%s)" % (out.get("holder"), out.get("reason")))
    if code != 200:
        raise StateError("状态服务 lease %s 回了 %d:%s" % (action, code, out.get("error")))
    return out


# -- 本地记录 -----------------------------------------------------------------------------------------

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _scan(root_fd: int) -> Tuple[Dict[str, List[int]], Dict[str, List[int]]]:
    """(普通文件 {arc: [大小, 修改时间纳秒]}, 库 {arc: [大小, 修改时间纳秒]});arc = 子目录/相对路径。"""
    files: Dict[str, List[int]] = {}
    dbs: Dict[str, List[int]] = {}
    for d in SYNC_DIRS:
        dfd = fdtree.open_path(root_fd, d, create=False)
        if dfd is None:
            continue
        try:
            for rel, meta in treetar.manifest(dfd, skip=_skip_journal).items():
                (dbs if _is_db(rel) else files)[d + "/" + rel] = meta
        finally:
            os.close(dfd)
    return files, dbs


def _chown_tree(root: str, uid: int, gid: int) -> None:
    """载入之后把属主交还给 agent。内容刚从状态服务解出来、还没有任何进程碰过,按路径走即可(不跟随链接)。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames + filenames:
            os.chown(os.path.join(dirpath, n), uid, gid, follow_symlinks=False)
    os.chown(root, uid, gid)


# -- 状态 ---------------------------------------------------------------------------------------------

def load(client: Client, local_root: str, owner: Optional[Tuple[int, int]] = None) -> int:
    os.makedirs(local_root, mode=0o700, exist_ok=True)
    conn, resp = client._open("GET", "/v1/state")
    try:
        if resp.status == 409:
            raise LeaseLost("载入时发现独占不在本沙箱手里")
        if resp.status != 200:
            raise StateError("状态服务载入回了 %d:%s" % (resp.status, resp.read()[:200].decode("utf-8", "replace")))
        root_fd = fdtree.open_root(local_root)
        try:
            res = treetar.unpack(resp, root_fd, allowed_top=lambda t: t in SYNC_DIRS, max_file=MAX_FILE,
                                 max_total=1 << 40, drop_db_journals=False)
        finally:
            os.close(root_fd)
    finally:
        conn.close()
    for d in SYNC_DIRS:
        os.makedirs(os.path.join(local_root, d), mode=0o700, exist_ok=True)
    if owner is not None:
        _chown_tree(local_root, *owner)
    with open(os.path.join(local_root, statesync.MARK_LOADED), "w") as f:
        f.write(str(int(time.time())))
    _write_json(os.path.join(local_root, RECORD), _baseline(local_root))
    return len(res.written)


def _baseline(local_root: str) -> dict:
    fd = fdtree.open_root(local_root)
    try:
        files, dbs = _scan(fd)
    finally:
        os.close(fd)
    sigs = {}
    for arc in dbs:
        try:
            sigs[arc] = statesync._db_signature(_path(local_root, arc))
        except OSError:
            pass
    return {"files": files, "dbs": sigs}


def _path(root: str, arc: str) -> pathlib.Path:
    return pathlib.Path(root, *arc.split("/"))


def sync(client: Client, local_root: str, final: bool = False) -> dict:
    """本地 → 状态服务。没有改动也没有删除时不发请求(final 时照发,保证收尾那一刻 NAS 与本地一致)。"""
    rec_path = os.path.join(local_root, RECORD)
    record = _read_json(rec_path)
    rec_files, rec_dbs = record.get("files") or {}, record.get("dbs") or {}
    root_fd = fdtree.open_root(local_root)
    try:
        files, dbs = _scan(root_fd)
        changed = {arc for arc, m in files.items() if rec_files.get(arc) != m and m[0] <= MAX_FILE}
        too_big = sorted(arc for arc, m in files.items() if m[0] > MAX_FILE)
        db_changed: Dict[str, List[int]] = {}
        for arc in dbs:
            try:
                sig = statesync._db_signature(_path(local_root, arc))
            except OSError:
                continue
            if not statesync._db_unchanged(sig, rec_dbs.get(arc), _path(local_root, arc)):
                db_changed[arc] = sig
        gone = (set(rec_files) - set(files)) | (set(rec_dbs) - set(dbs))
        loaded = os.path.exists(os.path.join(local_root, statesync.MARK_LOADED))
        if not (changed or db_changed or gone or final):
            return {"uploaded": 0, "skipped": True}
        keep: Dict[str, List[str]] = {d: [] for d in SYNC_DIRS}
        for arc in list(files) + list(dbs):
            top, _s, rel = arc.partition("/")
            keep[top].append(rel)
        tmpdir = os.path.join(local_root, TMP_DIR)
        os.makedirs(tmpdir, mode=0o700, exist_ok=True)
        fd, tar_path = tempfile.mkstemp(dir=tmpdir, suffix=".tar.gz")
        os.close(fd)
        packed: Dict[str, List[int]] = {}
        try:
            with tarfile.open(tar_path, "w:gz", compresslevel=1) as tar:
                treetar.add_meta(tar, "sync.json", {"keep": keep, "may_delete": loaded})
                by_dir: Dict[str, set] = {}
                for arc in changed:
                    top, _s, rel = arc.partition("/")
                    by_dir.setdefault(top, set()).add(rel)
                for top, rels in by_dir.items():
                    dfd = fdtree.open_path(root_fd, top, create=False)
                    if dfd is None:
                        continue
                    try:
                        for rel, meta in treetar.pack(tar, dfd, top + "/", only=rels).items():
                            packed[top + "/" + rel] = meta
                    finally:
                        os.close(dfd)
                for arc in db_changed:
                    snap = os.path.join(tmpdir, "db-%d" % len(packed))
                    try:
                        statesync.copy_db(_path(local_root, arc), _path(tmpdir, os.path.basename(snap)))
                        treetar.add_file(tar, arc, snap)
                    except Exception:            # noqa: BLE001 —— 一个库坏了不让整次同步停摆;下一轮再试
                        db_changed[arc] = None   # type: ignore[assignment]
                    finally:
                        if os.path.exists(snap):
                            os.unlink(snap)
            code, out = client.upload("PUT", "/v1/state", tar_path)
        finally:
            os.unlink(tar_path)
    finally:
        os.close(root_fd)
    if code == 409:
        raise LeaseLost("回写时发现独占已不在本沙箱手里(%s)" % out.get("reason"))
    if code != 200:
        raise StateError("状态服务回写回了 %d:%s" % (code, out.get("error")))
    new_files = {arc: m for arc, m in files.items() if arc not in changed or arc in packed}
    new_files.update(packed)
    new_dbs = {arc: rec_dbs[arc] for arc in dbs if arc in rec_dbs}
    new_dbs.update({arc: sig for arc, sig in db_changed.items() if sig is not None})
    _write_json(rec_path, {"files": new_files, "dbs": new_dbs})
    out["uploaded"] = len(packed) + sum(1 for s in db_changed.values() if s is not None)
    if too_big:
        out.setdefault("errors", []).append("超过单文件上限未回写:%s" % ", ".join(too_big[:3]))
    return out


def finish(client: Client, local_root: str, release: bool = False) -> dict:
    out = sync(client, local_root, final=True)
    code, fin = client.call_json("POST", "/v1/state/finish", {"release": release})
    if code == 409:
        raise LeaseLost("收尾时发现独占已不在本沙箱手里")
    if code != 200:
        raise StateError("状态服务收尾回了 %d:%s" % (code, fin.get("error")))
    out["finished"] = True
    return out


# -- 知识库 -------------------------------------------------------------------------------------------

def _kb_skip(rel: str) -> bool:
    return rel in (KB_LOCK, ".kb-version")


def kb_pull(client: Client, kb_dir: str, state_path: str, writable_by: Optional[Tuple[int, int]] = None) -> dict:
    """按清单增量拉取。writable_by 为 None 时拉下来的文件只读(诊断环境);给了就交给那个用户(导入环境)。"""
    code, man = client.get_json("/v1/kb/manifest")
    if code != 200:
        raise StateError("状态服务知识库清单回了 %d" % code)
    state = _read_json(state_path)
    server = state.get("server") or {}
    files = man.get("files") or {}
    need = sorted(rel for rel, m in files.items() if server.get(rel) != m)
    gone = sorted(rel for rel in server if rel not in files)
    os.makedirs(kb_dir, mode=0o755, exist_ok=True)
    kfd = fdtree.open_root(kb_dir)
    fetched = 0
    try:
        for i in range(0, len(need), PATHS_PER_REQUEST):
            batch = need[i:i + PATHS_PER_REQUEST]
            data = json.dumps({"paths": batch}).encode("utf-8")
            conn, resp = client._open("POST", "/v1/kb/files", body=data,
                                      headers={"Content-Type": "application/json"}, length=len(data))
            try:
                if resp.status != 200:
                    raise StateError("状态服务取知识库文件回了 %d" % resp.status)
                res = treetar.unpack(resp, kfd, allowed_top=lambda t: True, max_file=MAX_FILE, max_total=1 << 40,
                                     file_mode=0o644 if writable_by is None else 0o600,
                                     dir_mode=0o755 if writable_by is None else 0o700)
                fetched += len(res.written)
            finally:
                conn.close()
        for rel in gone:
            parent, _s, name = rel.rpartition("/")
            pfd = fdtree.open_path(kfd, parent, create=False)
            if pfd is not None:
                try:
                    fdtree.unlink(pfd, name)
                finally:
                    os.close(pfd)
        local = treetar.manifest(kfd, skip=_kb_skip)
    finally:
        os.close(kfd)
    if writable_by is not None and (need or gone):
        _chown_tree(kb_dir, *writable_by)
    _write_json(state_path, {"version": man.get("version", 0), "server": files, "local": local})
    return {"version": man.get("version", 0), "fetched": fetched, "removed": len(gone)}


def kb_locked(kb_dir: str, now: Optional[float] = None) -> bool:
    try:
        st = os.lstat(os.path.join(kb_dir, KB_LOCK))
    except FileNotFoundError:
        return False
    return (time.time() if now is None else now) - st.st_mtime < KB_LOCK_STALE_S


def kb_publish(client: Client, kb_dir: str, state_path: str) -> dict:
    """导入环境:把本地知识库相对上次拉取 / 发布的改动整体发布。库被锁住(正在导入)时跳过,下一轮再发。"""
    if kb_locked(kb_dir):
        return {"skipped": "locked"}
    state = _read_json(state_path)
    base_local = state.get("local") or {}
    kfd = fdtree.open_root(kb_dir)
    try:
        cur = treetar.manifest(kfd, skip=_kb_skip, skip_dir=lambda rel: rel == KB_DOCSETS)
        changed = sorted(rel for rel, m in cur.items() if base_local.get(rel) != m)
        removed = sorted(rel for rel in base_local if rel not in cur and not rel.startswith(KB_DOCSETS + "/"))
        if not changed and not removed:
            return {"skipped": "unchanged"}
        tmpdir = os.path.join(os.path.dirname(state_path), TMP_DIR)
        os.makedirs(tmpdir, mode=0o700, exist_ok=True)
        fd, tar_path = tempfile.mkstemp(dir=tmpdir, suffix=".tar.gz")
        os.close(fd)
        try:
            with tarfile.open(tar_path, "w:gz", compresslevel=1) as tar:
                treetar.add_meta(tar, "publish.json", {"keep": sorted(cur)})
                treetar.pack(tar, kfd, "", only=set(changed), skip=_kb_skip, skip_dir=lambda rel: rel == KB_DOCSETS)
            code, out = client.upload("POST", "/v1/kb/publish", tar_path,
                                      headers={HDR_KB_BASE: str(state.get("version", 0))})
        finally:
            os.unlink(tar_path)
    finally:
        os.close(kfd)
    if code == 409:
        return {"stale": True, "server_version": out.get("version")}
    if code != 200:
        raise StateError("状态服务发布知识库回了 %d:%s" % (code, out.get("error")))
    c2, man = client.get_json("/v1/kb/manifest")
    docsets = {k: v for k, v in base_local.items() if k.startswith(KB_DOCSETS + "/")}
    _write_json(state_path, {"version": out["version"], "server": (man.get("files") or {}) if c2 == 200 else {},
                             "local": dict(cur, **docsets)})
    return {"version": out["version"], "published": len(changed), "removed": len(removed)}


def cleanup_tmp(local_root: str) -> None:
    shutil.rmtree(os.path.join(local_root, TMP_DIR), ignore_errors=True)
