"""跑题配置:读 ~/.acceptance/run.yaml(样例 acceptance/run.example.yaml),校验必填项,路径展开好。"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_PATH = pathlib.Path("~/.acceptance/run.yaml").expanduser()


class ConfigError(Exception):
    pass


def _path(raw: Any, base: pathlib.Path = REPO) -> pathlib.Path:
    p = pathlib.Path(str(raw)).expanduser()
    return p if p.is_absolute() else base / p


@dataclass(frozen=True)
class Judge:
    base_url: str
    model: str
    api_key_from: pathlib.Path
    temperature: float = 0.0
    concurrency: int = 6                 # 裁判模型(推理型)一次要一两分钟,并发开大一点


@dataclass(frozen=True)
class Config:
    bank: str
    out_dir: pathlib.Path
    names: Mapping[str, str]
    user_portal: str
    admin: Mapping[str, Any]
    kubectl_context: str
    namespace: str
    faultlab_dir: pathlib.Path
    faultlab_python: pathlib.Path
    live_delay_s: int
    kb_nas: pathlib.Path
    kb_pod: str
    kb_swap: bool
    kb_backup: pathlib.Path
    kb_local: pathlib.Path
    mock_wiki_port: int
    judge: Optional[Judge]
    concurrency: int = 3
    question_timeout_s: int = 900
    old_days: int = 60
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def admin_portal(self) -> str:
        return "http://127.0.0.1:%d" % int(self.admin["port"])


def _need(d: Mapping[str, Any], key: str, where: str) -> Any:
    if d.get(key) in (None, ""):
        raise ConfigError("配置缺 %s%s" % (where, key))
    return d[key]


def parse(raw: Dict[str, Any]) -> Config:
    names = _need(raw, "names", "")
    for k in ("instance", "alias", "broken"):
        _need(names, k, "names.")
    admin = _need(raw, "admin", "")
    for k in ("sso_sim", "port", "gateway_port", "user", "trust_file"):
        _need(admin, k, "admin.")
    admin = dict(admin, sso_sim=_path(admin["sso_sim"]), trust_file=_path(admin["trust_file"]),
                 roles=admin.get("roles") or "kb-admin")
    kube, lab, kb = raw.get("kubectl") or {}, _need(raw, "faultlab", ""), _need(raw, "kb", "")
    j = raw.get("judge") or {}
    judge = Judge(str(j["base_url"]).rstrip("/"), str(j["model"]), _path(j["api_key_from"]),
                  float(j.get("temperature") or 0), max(1, int(j.get("concurrency") or 6))) \
        if j.get("base_url") and j.get("model") else None
    return Config(
        bank=str(raw.get("bank") or "v1"), out_dir=_path(raw.get("out_dir") or "~/.acceptance/runs"),
        names={k: str(v) for k, v in names.items()}, user_portal=str(_need(raw, "user_portal", "")).rstrip("/"),
        admin=admin, kubectl_context=str(kube.get("context") or "orbstack"),
        namespace=str(kube.get("namespace") or "gaussdb-agent"),
        faultlab_dir=_path(_need(lab, "dir", "faultlab.")), faultlab_python=_path(_need(lab, "python", "faultlab.")),
        live_delay_s=int(lab.get("live_delay_s") or 180),
        kb_nas=_path(_need(kb, "nas_path", "kb.")), kb_pod=str(kb.get("pod_path") or "/nas/kb"),
        kb_swap=bool(kb.get("swap", True)), kb_backup=_path(_need(kb, "backup_dir", "kb.")),
        kb_local=_path(kb.get("local_dir") or "~/.acceptance/kb"),
        mock_wiki_port=int(raw.get("mock_wiki_port") or 18300), judge=judge,
        concurrency=max(1, int(raw.get("concurrency") or 3)),
        question_timeout_s=int(raw.get("question_timeout_s") or 900), old_days=int(raw.get("old_days") or 60),
        raw=raw)


def load(path: pathlib.Path = DEFAULT_PATH) -> Config:
    if not path.is_file():
        raise ConfigError("没有配置文件 %s:复制 acceptance/run.example.yaml 过去再改" % path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError("%s 不是合法的 YAML:%s" % (path, e)) from e
    try:
        return parse(raw)
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError("%s 写得不对:%s" % (path, e)) from e
