"""换了 embedding 模型但维度没变 —— 必须挡住(2026-09-21)。

`kb_meta` 原来记了 dims / engine / vector_engine / schema_version,**唯独没记模型名**。
于是:客户把 bge-m3 换成另一个同样 1024 维的模型、不加 --rebuild,库里就混着两套不同
向量空间的向量。跨空间算余弦相似度没有意义,检索质量悄悄下降,而状态行仍然显示
「覆盖 100%」——所有肉眼可见的指标都是正常的。

维度变了反而安全:embed.py 会校验 len(arr) != dims 并当作失败,索引输出里直接是
「失败 N · 覆盖 X/Y」。危险的恰恰是「维度一样、模型不同」这一种。

对策:把模型名记进 kb_meta,索引时比对;不一致就拒绝增量索引并说明要 --rebuild。
`--rebuild` 会清空重灌,所以那条路径上允许换。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import store as kbstore, store_sqlite  # noqa: E402


class _FakeStore(store_sqlite.SqliteStore):
    """只替掉 kb_meta 的读写,其余逻辑照跑。"""

    def __init__(self, recorded=None):
        self._meta = dict(recorded or {})

    def _meta_get(self, key):
        return self._meta.get(key)

    def _meta_set(self, key, value):
        self._meta[key] = value


def test_first_index_records_the_model():
    st = _FakeStore()
    st.check_embed_model("bge-m3", rebuild=False)
    assert st._meta_get("embed_model") == "bge-m3"


def test_same_model_passes():
    st = _FakeStore({"embed_model": "bge-m3"})
    st.check_embed_model("bge-m3", rebuild=False)          # 不抛就是通过


def test_changed_model_is_refused():
    """**核心**:同维度换模型,增量索引必须拒绝,不能默默写进去。"""
    st = _FakeStore({"embed_model": "bge-m3"})
    with pytest.raises(kbstore.StoreError) as exc:
        st.check_embed_model("gte-large", rebuild=False)
    msg = str(exc.value)
    assert "bge-m3" in msg and "gte-large" in msg, "要把两个模型名都说出来,否则没法判断该不该换"
    assert "--rebuild" in msg, "要告诉用户怎么继续"


def test_rebuild_allows_the_switch_and_updates_the_record():
    """--rebuild 会清空重灌,换模型在这条路径上是安全的。"""
    st = _FakeStore({"embed_model": "bge-m3"})
    st.check_embed_model("gte-large", rebuild=True)
    assert st._meta_get("embed_model") == "gte-large"


def test_legacy_store_without_the_record_adopts_current_model():
    """加这个特性之前建的库没有这一行 —— 当作首次记录,不要把老库判成「换过模型」。"""
    st = _FakeStore({"dims": "1024"})
    st.check_embed_model("bge-m3", rebuild=False)
    assert st._meta_get("embed_model") == "bge-m3"
