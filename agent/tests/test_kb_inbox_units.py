"""common.kb.inbox —— 知识库导入的收件目录与「文件不在沙箱里」的说明。

现场(客户 09-08 晚截图):用户说「文件在 D:\\AI\\…md,导入知识库」。skill 跑在 Linux 沙箱里,读不到用户电脑上的文件;
模型靠客户自己的 AGENTS.md 推理出「要上传」。这层推理该由脚本给:本机路径直接说要上传到哪个目录、命令怎么写,
并按文件名在沙箱常见目录里找一遍同名文件(前端可能已经传上来了)。
"""
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import inbox  # noqa: E402


@pytest.mark.parametrize("p", [r"D:\AI\故障分析报告.md", "C:/Users/a/x.docx", r"\\srv\share\x.md",
                               "~/Desktop/x.md", "/Users/me/Documents/x.pdf", "/home/me/Downloads/x.xlsx"])
def test_local_machine_paths_are_recognised(p):
    assert inbox.looks_local(p)


@pytest.mark.parametrize("p", ["/workspace/docs/x.md", "docs/x.md", "x.md", "./inbox/uploads/x.md", "/home/me/kb/x.md"])
def test_sandbox_paths_are_not_flagged(p):
    assert not inbox.looks_local(p)


def test_basename_splits_on_both_separators():
    assert inbox.basename(r"D:\AI\故障分析报告-20250120.md") == "故障分析报告-20250120.md"
    assert inbox.basename("/workspace/docs/x.md") == "x.md"


def test_inbox_dir_defaults_under_kb_and_env_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("GSDB_KB_INBOX", raising=False)
    assert inbox.inbox_dir(tmp_path / "kb") == tmp_path / "kb" / "inbox" / "uploads"
    monkeypatch.setenv("GSDB_KB_INBOX", str(tmp_path / "docs"))
    assert inbox.inbox_dir(tmp_path / "kb") == tmp_path / "docs"


def test_find_same_name_scans_candidate_dirs_in_order(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "x.md").write_text("1", encoding="utf-8")
    (b / "x.md").write_text("2", encoding="utf-8")
    assert inbox.find_same_name("x.md", [b, a, tmp_path / "missing"]) == [b / "x.md", a / "x.md"]


def test_message_for_local_path_tells_user_to_upload(tmp_path, monkeypatch):
    monkeypatch.delenv("GSDB_KB_INBOX", raising=False)
    kb = tmp_path / "kb"
    msg = inbox.missing_file_message(r"D:\AI\故障分析报告.md", kb, cwd=tmp_path)
    assert "文件不存在" in msg and "沙箱" in msg and "上传" in msg
    assert str(kb / "inbox" / "uploads") in msg
    assert "ingest" in msg and "故障分析报告.md" in msg
    assert "同名文件" not in msg


def test_message_lists_same_name_files_already_in_sandbox(tmp_path, monkeypatch):
    monkeypatch.delenv("GSDB_KB_INBOX", raising=False)
    kb = tmp_path / "kb"
    up = kb / "inbox" / "uploads"
    up.mkdir(parents=True)
    (up / "故障分析报告.md").write_text("x", encoding="utf-8")
    msg = inbox.missing_file_message(r"D:\AI\故障分析报告.md", kb, cwd=tmp_path)
    assert "同名文件" in msg and str(up / "故障分析报告.md") in msg


def test_message_for_sandbox_path_does_not_blame_local_machine(tmp_path, monkeypatch):
    monkeypatch.delenv("GSDB_KB_INBOX", raising=False)
    msg = inbox.missing_file_message("/workspace/docs/nope.md", tmp_path / "kb", cwd=tmp_path)
    assert "文件不存在" in msg and "本机" not in msg
    assert "inbox" in msg          # 仍告诉用户收件目录在哪
