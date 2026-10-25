"""OpenAI 兼容 /v1/embeddings 客户端 —— 逐批、失败隔离、按内容哈希缓存。

opendb-harness 的教训直接写进设计:它一次性 embed 整篇,任一批超时整篇落 NULL,
且没有补齐任务,结果向量覆盖率 14% 还没人发现。这里:
  · 每批独立,失败只影响这一批(返回 None),其余照常;
  · 结果按 data[i].index 归位、校验维度与有限性(乱序/缺项/NaN 都当失败);
  · 缓存按 sha256(model + text) 落盘,重建索引不重算;
  · 密钥只在内存,不进日志。
向量端点来自 OpenCode 自己的 provider(config.opencode_provider)或显式 url。
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
import pathlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Sequence

from . import config as kbconfig


class EmbedError(Exception):
    """配置层错误(端点缺失等)。运行时单批失败不抛,返回 None。"""


# 不走系统 HTTP 代理(同 store_graph:macOS 的 urllib 会把 127.0.0.1 也交给代理)。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class EmbedStats:
    requested: int = 0
    embedded: int = 0
    cached: int = 0
    failed: int = 0
    last_error: str = ""


class Embedder:
    def __init__(self, base_url: str, api_key: str, model: str, dims: int,
                 timeout_s: float = 30.0, batch: int = 16,
                 cache_dir: Optional[pathlib.Path] = None):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.dims = int(dims)
        self.timeout_s = float(timeout_s)
        self.batch = max(1, int(batch))
        self.cache_dir = pathlib.Path(cache_dir) if cache_dir else None
        self.stats = EmbedStats()

    def __repr__(self) -> str:
        return f"Embedder(base_url={self.base_url!r}, model={self.model!r}, dims={self.dims})"

    @classmethod
    def from_config(cls, cfg: kbconfig.KbConfig) -> Optional["Embedder"]:
        """source=none → None(调用方据此写"向量:未启用");其余按来源取端点。"""
        e = cfg.embeddings
        if e.source == "none":
            return None
        if e.source == "opencode":
            ep = kbconfig.opencode_provider()
            base_url, key = ep.base_url, ep.api_key
        else:
            base_url = e.base_url
            key = os.environ.get(e.api_key_env, "") if e.api_key_env else ""
        if not base_url:
            raise EmbedError("embedding 端点为空")
        return cls(base_url, key, e.model, e.dims, timeout_s=e.timeout_s, batch=e.batch,
                   cache_dir=cfg.kb_dir / "index" / "embed-cache")

    # --- 缓存 -------------------------------------------------------------

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256((self.model + "\x00" + text).encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Optional[pathlib.Path]:
        if self.cache_dir is None:
            return None
        return self.cache_dir / key[:2] / (key + ".f64")   # float64 无损;float32 读回来会对不上

    def _cache_get(self, text: str) -> Optional[List[float]]:
        path = self._cache_path(self._cache_key(text))
        if path is None or not path.is_file():
            return None
        try:
            arr = array.array("d")
            arr.frombytes(path.read_bytes())
        except (OSError, ValueError):
            return None
        if len(arr) != self.dims:
            return None
        return list(arr)

    def _cache_put(self, text: str, vec: Sequence[float]) -> None:
        path = self._cache_path(self._cache_key(text))
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(array.array("d", [float(v) for v in vec]).tobytes())
            tmp.replace(path)
        except OSError:
            pass                                  # 缓存写不进只是慢,不是错

    # --- HTTP -------------------------------------------------------------

    def _post(self, texts: Sequence[str], timeout_s: float) -> List[List[float]]:
        body = json.dumps({"model": self.model, "input": list(texts)}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(f"{self.base_url}/embeddings", data=body,
                                     method="POST", headers=headers)
        with _OPENER.open(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return parse_embeddings(payload, len(texts), self.dims)

    def _post_with_retry(self, texts: Sequence[str], timeout_s: float) -> List[List[float]]:
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                return self._post(texts, timeout_s)
            except urllib.error.HTTPError as exc:
                last = exc
                if 400 <= exc.code < 500:
                    break                         # 4xx 重试没意义(鉴权/模型名错)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = exc
            if attempt == 0:
                time.sleep(0.2)
        raise EmbedError(_describe(last))

    # --- 公开 API -----------------------------------------------------------

    def embed(self, texts: Sequence[str], timeout_s: Optional[float] = None) -> List[Optional[List[float]]]:
        """逐条返回向量或 None;缓存命中的不发请求;失败按批隔离并记进 stats。"""
        budget = float(timeout_s) if timeout_s is not None else self.timeout_s
        out: List[Optional[List[float]]] = [None] * len(texts)
        pending: List[int] = []
        cached = 0
        for i, t in enumerate(texts):
            hit = self._cache_get(t)
            if hit is not None:
                out[i] = hit
                cached += 1
            else:
                pending.append(i)

        embedded = failed = 0
        last_error = ""
        for start in range(0, len(pending), self.batch):
            idxs = pending[start:start + self.batch]
            try:
                vecs = self._post_with_retry([texts[i] for i in idxs], budget)
            except EmbedError as exc:
                failed += len(idxs)
                last_error = str(exc)
                continue
            for i, vec in zip(idxs, vecs):
                out[i] = vec
                self._cache_put(texts[i], vec)
                embedded += 1
        self.stats = EmbedStats(requested=len(texts), embedded=embedded, cached=cached,
                                failed=failed, last_error=last_error)
        return out

    def embed_one(self, text: str, timeout_s: Optional[float] = None) -> Optional[List[float]]:
        return self.embed([text], timeout_s=timeout_s)[0]


def _describe(exc: Optional[Exception]) -> str:
    if exc is None:
        return "未知错误"
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return f"HTTP {exc.code} {exc.reason} {detail}".strip()
    return f"{type(exc).__name__}: {exc}"


def parse_embeddings(payload: dict, expect_n: int, expect_dims: int) -> List[List[float]]:
    """按 index 归位并校验:条数、维度、有限性,缺一不可(纯函数,单测钉死)。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != expect_n:
        raise ValueError(f"embeddings 返回 {len(data) if isinstance(data, list) else '非列表'} 条,期望 {expect_n}")
    slots: List[Optional[List[float]]] = [None] * expect_n
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("embeddings data 元素不是对象")
        idx = item.get("index")
        vec = item.get("embedding")
        if not isinstance(idx, int) or not (0 <= idx < expect_n) or slots[idx] is not None:
            raise ValueError(f"embeddings index 非法或重复:{idx!r}")
        if not isinstance(vec, list) or len(vec) != expect_dims:
            raise ValueError(f"embeddings 第 {idx} 条维度 {len(vec) if isinstance(vec, list) else '?'},期望 {expect_dims}")
        floats = []
        for v in vec:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
                raise ValueError(f"embeddings 第 {idx} 条含非有限数")
            floats.append(float(v))
        slots[idx] = floats
    # 逐槽检查(不用 any/all 的稀疏数组语义——opendb-harness 在 JS 里踩过)
    for i in range(expect_n):
        if slots[i] is None:
            raise ValueError(f"embeddings 缺第 {i} 条")
    return [s for s in slots if s is not None]
