"""common.kb.text —— 分词(纯函数,无库)。

不走任何引擎的文本解析器:常见的默认分词都不做中文分词、对标点的切分也各不相同。这里在 Python 侧
把文本切成「中文二元组 + 标识符整词 + 碎片」,SQLite 索引与文件模式都直接存、直接比这些 token,
行为可被单测钉死。
"""
import pathlib
import sys


_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import text  # noqa: E402


# ---------------------------------------------------------------- tokenize

def test_chinese_becomes_bigrams():
    assert text.tokenize("偶现单条") == ["偶现", "现单", "单条"]


def test_single_chinese_char_is_kept_as_is():
    """长度 1 的中文段没有二元组可切,丢掉它等于这段永远搜不到。"""
    assert text.tokenize("表 慢") == ["表", "慢"]


def test_identifiers_stay_whole_and_lowercase():
    """对象名、GUC、等待事件是查询的主入口,拆碎了就命不中。"""
    toks = text.tokenize("针对 cbst.cosp_asyn_task_dtl 调大 autovacuum_vacuum_threshold")
    assert "cbst.cosp_asyn_task_dtl" in toks
    assert "autovacuum_vacuum_threshold" in toks
    assert "针对" in toks and "调大" in toks


def test_identifier_parts_are_also_emitted():
    """`cbst.cosp_asyn_task_dtl` 既要整词命中,也要能被「cosp_asyn_task_dtl」「dtl」找到。"""
    toks = text.tokenize("cbst.cosp_asyn_task_dtl")
    assert "cosp_asyn_task_dtl" in toks
    assert "dtl" in toks


def test_wait_event_with_colon():
    toks = text.tokenize("LWLock:WALWriteLock 等待冲高")
    assert "lwlock:walwritelock" in toks
    assert "walwritelock" in toks


def test_mixed_script_boundaries_do_not_bleed():
    """中英混排:二元组不得跨过英文/标点,否则会造出「秒s」这种永远匹配不上的词。"""
    toks = text.tokenize("耗时3s问题")
    assert "耗时" in toks and "问题" in toks and "3s" in toks
    assert not any(t in ("时3", "s问") for t in toks)


def test_punctuation_is_a_boundary():
    toks = text.tokenize("现象:update慢,原因:锁")
    assert "现象" in toks and "update" in toks and "原因" in toks and "锁" in toks


def test_tokenize_is_deterministic_and_order_preserving():
    assert text.tokenize("a b a") == ["a", "b", "a"]


# ---------------------------------------------------------------- query tokens

def test_query_tokens_from_text_drop_stop_bigrams():
    """「的」「了」这类字和它们组成的二元组只会拉低排序,查询侧不要。"""
    toks = text.query_tokens("表的膨胀了")
    assert "膨胀" in toks
    assert "的膨" not in toks and "胀了" not in toks


# ---------------------------------------------------------------- normalize

def test_normalize_fullwidth_and_case():
    assert text.normalize("ＡＢＣ　ｘ") == "abc x"


def test_normalize_collapses_whitespace():
    assert text.normalize("a \n\t b") == "a b"
