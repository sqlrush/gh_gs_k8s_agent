"""知识库写入互斥 + 原子写:导入 Pod 与读 Pod 共享 NAS 目录,写一半的清单不能被读到,两个 apply 不能互相覆盖。"""
import json
import pathlib
import sys
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from common.kb import atomic, lock  # noqa: E402


def test_hold_creates_and_removes_lock_file(tmp_path):
    with lock.hold(tmp_path):
        data = json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))
        assert ":" in data["owner"] and isinstance(data["ts"], (int, float))
    assert not (tmp_path / ".lock").exists()


def test_second_holder_is_refused_with_a_clear_message(tmp_path):
    with lock.hold(tmp_path):
        with pytest.raises(lock.KbLocked) as exc:
            with lock.hold(tmp_path):
                pass
        assert (tmp_path / ".lock").exists()      # 被拒的一方不能顺手把别人的锁删了
    assert "正被" in str(exc.value) and "稍后" in str(exc.value)
    assert not (tmp_path / ".lock").exists()      # 持有者退出后才释放


def test_stale_lock_is_taken_over(tmp_path):
    (tmp_path / ".lock").write_text(json.dumps({"owner": "dead:1", "ts": time.time() - 3600}), encoding="utf-8")
    with lock.hold(tmp_path, ttl_s=600):
        assert json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))["owner"] != "dead:1"


def test_garbage_lock_file_is_treated_as_stale(tmp_path):
    (tmp_path / ".lock").write_text("not json", encoding="utf-8")
    with lock.hold(tmp_path):
        pass
    assert not (tmp_path / ".lock").exists()


def test_lock_is_released_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with lock.hold(tmp_path):
            raise RuntimeError("boom")
    assert not (tmp_path / ".lock").exists()


def test_write_text_atomic_leaves_no_partial_file(tmp_path):
    target = tmp_path / "INDEX.md"
    target.write_text("old", encoding="utf-8")
    atomic.write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("*.tmp"))


def test_write_text_atomic_creates_parent_dirs(tmp_path):
    target = tmp_path / "cases" / "S1-x.md"
    atomic.write_text_atomic(target, "body")
    assert target.read_text(encoding="utf-8") == "body"
