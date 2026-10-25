"""知识库配置:<kb>/kb.yaml + OpenCode provider 端点 + 凭据名。

口令永远不在这里:kb.yaml 只写连接元数据与**凭据名**,真正的口令在
$GSDB_HOME/credentials/<name>.enc(common.credential)。kb.yaml 里出现 password
直接报错——它会被 cat、进备份、贴进工单。

OpenCode provider:`embeddings.source: opencode` 时复用 OpenCode 自己的模型端点
(~/.config/opencode/opencode.jsonc 的 provider 段),向同一个 base URL 请求
/embeddings。API key 只进内存,ProviderEndpoint 的 repr 不带它。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import yaml

_HERE = pathlib.Path(__file__).resolve()
_ENV_TEMPLATE_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_EMBED_SOURCES = ("none", "opencode", "url")
_SECRET_KEYS = frozenset({"password", "passwd", "secret", "apikey", "api_key", "token"})


class KbConfigError(Exception):
    """配置层的用户可读错误。"""


# ---------------------------------------------------------------- kb dir

def install_root(start: Optional[pathlib.Path] = None) -> pathlib.Path:
    """skills/ 的父目录(装好后 common/ 也在 skills/ 下);源码仓里是仓库根。"""
    here = (start or _HERE).resolve()
    for anc in here.parents:
        if anc.name == "skills":
            return anc.parent
    for anc in here.parents:
        if anc.name == "common":
            return anc.parent
    return here.parent.parent.parent


def resolve_kb_dir(cli_value: Optional[str]) -> pathlib.Path:
    """--kb > $GSDB_KB_DIR > <安装根>/kb(与 skills/ 同级,重装不删)。"""
    if cli_value:
        return pathlib.Path(cli_value).expanduser()
    env_dir = os.environ.get("GSDB_KB_DIR")
    if env_dir:
        return pathlib.Path(env_dir).expanduser()
    return install_root() / "kb"


# ---------------------------------------------------------------- jsonc

def strip_jsonc(src: str) -> str:
    """去掉 // 与 /* */ 注释和尾逗号,字符串内的内容一字不动。"""
    out: list = []
    i, n = 0, len(src)
    in_str = False
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in "}]":
            k = len(out) - 1
            while k >= 0 and out[k].isspace():
                k -= 1
            if k >= 0 and out[k] == ",":
                del out[k]
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- opencode provider

@dataclass(frozen=True)
class ProviderEndpoint:
    provider: str
    base_url: str
    api_key: str = field(default="", repr=False)   # repr 不带密钥


def _opencode_config_path(home: pathlib.Path) -> Optional[pathlib.Path]:
    explicit = os.environ.get("OPENCODE_CONFIG")
    if explicit:
        return pathlib.Path(explicit).expanduser()
    for name in ("opencode.jsonc", "opencode.json"):
        p = home / ".config" / "opencode" / name
        if p.is_file():
            return p
    return None


def _expand_env(value: str) -> str:
    def sub(m: "re.Match") -> str:
        got = os.environ.get(m.group(1))
        if got is None:
            raise KbConfigError(f"OpenCode 配置引用了环境变量 {m.group(1)},但它没有设置")
        return got
    return _ENV_TEMPLATE_RE.sub(sub, value)


def _auth_json_key(home: pathlib.Path, provider: str) -> str:
    path = home / ".local" / "share" / "opencode" / "auth.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    entry = data.get(provider) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("key") or entry.get("apiKey") or "")


def opencode_provider(model: Optional[str] = None,
                      home: Optional[pathlib.Path] = None) -> ProviderEndpoint:
    """OpenCode 当前模型所在 provider 的 base URL 与 key。

    model 形如 "kimi/k3";不给则用配置里的默认 model。
    """
    home = home or pathlib.Path.home()
    path = _opencode_config_path(home)
    if path is None or not path.is_file():
        raise KbConfigError(
            "找不到 opencode 配置(~/.config/opencode/opencode.jsonc);"
            "embeddings.source: opencode 需要它,或改用 source: url 显式给端点")
    try:
        cfg = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise KbConfigError(f"opencode 配置 {path} 解析失败:{exc}") from exc

    spec = model or str(cfg.get("model") or "")
    if "/" not in spec:
        raise KbConfigError(f"opencode 配置的 model {spec!r} 不是 provider/model 形式")
    provider = spec.split("/", 1)[0]
    pconf = (cfg.get("provider") or {}).get(provider)
    if not isinstance(pconf, dict):
        raise KbConfigError(f"opencode 配置里没有 provider {provider!r} 的定义")
    options = pconf.get("options") or {}
    base_url = str(options.get("baseURL") or options.get("baseUrl") or "").rstrip("/")
    if not base_url:
        raise KbConfigError(f"provider {provider!r} 没有 options.baseURL,拿不到端点")
    api_key = _expand_env(str(options.get("apiKey") or "")) or _auth_json_key(home, provider)
    return ProviderEndpoint(provider=provider, base_url=base_url, api_key=api_key)


# ---------------------------------------------------------------- kb.yaml

@dataclass(frozen=True)
class GraphStore:
    url: str
    user: str
    credential: str
    database: str = "neo4j"


@dataclass(frozen=True)
class StoreConfig:
    graph: Optional[GraphStore] = None
    # 2026-10-23 高斯 / PG 向量库下线,向量与词法索引改为 <kb>/index/store.sqlite(不用配)。
    # kb.yaml 里还留着 store.pg 时不报错(升级不能因为一段旧配置就让知识库用不了),只记一笔,
    # 由 health / index 提示可以删掉。
    legacy_pg: bool = False


@dataclass(frozen=True)
class EmbeddingConfig:
    source: str = "none"            # none | opencode | url
    model: str = "bge-m3"
    dims: int = 1024
    base_url: str = ""              # source=url 时必填
    api_key_env: str = ""           # source=url 时可选:key 所在环境变量名
    timeout_s: float = 30.0         # 索引侧单批超时
    query_timeout_s: float = 1.5    # 查询侧超时,超了本次不用向量
    batch: int = 16


@dataclass(frozen=True)
class Thresholds:
    """分类型分数阈值(RRF 融合后的归一分),低于阈值整类返回「无」。"""
    clause: float = 0.25
    case: float = 0.25
    path: float = 0.25
    chunk: float = 0.20
    symptom: float = 0.25          # 现象节点命中门槛,过了才去图里走路径
    lexical_min: float = 0.01      # 词法原始分下限(ts_rank_cd 归一),防"排第一但其实不相关"
    vector_min: float = 0.45       # 向量相似度下限(1 - 余弦距离)
    # 绝对相关度:命中的强 token(标识符 / 代码 / ≥6 字符的英文词 / 连成 ≥4 字的中文短语)至少 1 个,且覆盖查询 token 的比例达标;
    # 相对相关度:词法原始分不低于同类最高分的这个比例——RRF 是按排名归一的,第 2、3 名分数照样接近 1,
    # 没有这两道门,一个泛词("等待""占")命中的条款也会上榜。
    min_coverage: float = 0.12
    relative_floor: float = 0.45
    node_relative_floor: float = 0.5
    top_clause: int = 3
    top_case: int = 3
    top_path: int = 2
    top_raw: int = 2
    top_guide: int = 2
    top_typed: int = 2             # 手册 / 缺陷 / 基线 / 产品文档 / Wiki 每类最多几条(2026-10-24 八类知识)


@dataclass(frozen=True)
class KbConfig:
    kb_dir: pathlib.Path
    store: StoreConfig
    embeddings: EmbeddingConfig
    thresholds: Thresholds
    defaults: Dict[str, Any]
    # 测试开关(只许在测试环境的 kb.yaml 里开;health 每次提示,标准知识库打包时开着就拒绝)
    #   docset_applicability_unknown: true —— 官方文档集对任何实例都显示「适用性未知」(2026-10-25 用户选 c)
    test_switches: Dict[str, Any] = field(default_factory=dict)

    @property
    def docset_applicability_unknown(self) -> bool:
        return bool(self.test_switches.get("docset_applicability_unknown"))


def _reject_secrets(node: Any, path: str) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in _SECRET_KEYS:
                raise KbConfigError(
                    f"kb.yaml 的 {path}.{k} 是明文口令/密钥——不允许。"
                    f"口令请用 `python3 -m common.credential_cli set <凭据名>` 加密保存,"
                    f"kb.yaml 里只写 credential: <凭据名>")
            _reject_secrets(v, f"{path}.{k}")


def _graph_store(raw: Any) -> Optional[GraphStore]:
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise KbConfigError("kb.yaml store.graph 必须是键值映射")
    for key in ("url", "user", "credential"):
        if not raw.get(key):
            raise KbConfigError(f"kb.yaml store.graph 缺少 {key}")
    return GraphStore(url=str(raw["url"]).rstrip("/"), user=str(raw["user"]),
                      credential=str(raw["credential"]),
                      database=str(raw.get("database") or "neo4j"))


def _embeddings(raw: Any) -> EmbeddingConfig:
    if not raw:
        return EmbeddingConfig()
    if not isinstance(raw, dict):
        raise KbConfigError("kb.yaml embeddings 必须是键值映射")
    source = str(raw.get("source") or "none").lower()
    if source not in _EMBED_SOURCES:
        raise KbConfigError(f"kb.yaml embeddings.source 只能是 {'/'.join(_EMBED_SOURCES)},拿到 {source!r}")
    base_url = str(raw.get("base_url") or "").rstrip("/")
    if source == "url" and not base_url:
        raise KbConfigError("kb.yaml embeddings.source: url 需要 base_url")
    return EmbeddingConfig(
        source=source,
        model=str(raw.get("model") or "bge-m3"),
        dims=int(raw.get("dims") or 1024),
        base_url=base_url,
        api_key_env=str(raw.get("api_key_env") or ""),
        timeout_s=float(raw.get("timeout_s") or 30.0),
        query_timeout_s=float(raw.get("query_timeout_s") or 1.5),
        batch=int(raw.get("batch") or 16),
    )


def _thresholds(raw: Any) -> Thresholds:
    if not raw:
        return Thresholds()
    if not isinstance(raw, dict):
        raise KbConfigError("kb.yaml thresholds 必须是键值映射")
    base = Thresholds()
    kwargs = {}
    for name in ("clause", "case", "path", "chunk", "symptom", "lexical_min", "vector_min",
                 "min_coverage", "relative_floor", "node_relative_floor"):
        kwargs[name] = float(raw.get(name, getattr(base, name)))
    for name in ("top_clause", "top_case", "top_path", "top_raw", "top_guide", "top_typed"):
        kwargs[name] = int(raw.get(name, getattr(base, name)))
    return Thresholds(**kwargs)


def load(kb_dir: pathlib.Path) -> KbConfig:
    """读 <kb>/kb.yaml;文件不存在 = 全默认(无图库、无向量),不是错误。"""
    path = pathlib.Path(kb_dir) / "kb.yaml"
    raw: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise KbConfigError(f"kb.yaml 解析失败:{exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise KbConfigError("kb.yaml 顶层必须是键值映射")
        raw = loaded or {}
    _reject_secrets(raw, "kb.yaml")
    store_raw = raw.get("store") or {}
    if not isinstance(store_raw, dict):
        raise KbConfigError("kb.yaml store 必须是键值映射")
    return KbConfig(
        kb_dir=pathlib.Path(kb_dir),
        store=StoreConfig(graph=_graph_store(store_raw.get("graph")),
                          legacy_pg=bool(store_raw.get("pg"))),
        embeddings=_embeddings(raw.get("embeddings")),
        thresholds=_thresholds(raw.get("thresholds")),
        defaults=dict(raw.get("defaults") or {}),
        test_switches=_test_switches(raw.get("test_switches")),
    )


TEST_SWITCHES = ("docset_applicability_unknown",)


def _test_switches(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise KbConfigError("kb.yaml test_switches 必须是键值映射")
    unknown = [k for k in raw if k not in TEST_SWITCHES]
    if unknown:
        raise KbConfigError(f"kb.yaml test_switches 不认识 {'、'.join(map(str, unknown))}(只有 {'、'.join(TEST_SWITCHES)})")
    return {k: v for k, v in raw.items() if v}
