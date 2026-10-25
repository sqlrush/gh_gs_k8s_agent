"""HEIC / TIFF 转 JPEG 副本(common/kb/imgconv.py),以及 kb-init scan 把工作单的 image_path 指向副本。

2026-10-22 用户反馈:一批 HEIC 照片一张都没读出来——read 打不开 HEIC,模型只能让用户自己转格式。
"""
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "skills" / "gaussdb-kb-init" / "scripts"))

from common.kb import imgconv  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _tiff(path, size=(3000, 1200), orientation=None):
    im = Image.new("RGB", size, (200, 30, 30))
    kw = {"tiffinfo": {274: orientation}} if orientation else {}
    im.save(path, "TIFF", **kw)
    return path


def test_formats_read_can_open_are_left_alone(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGB", (10, 10)).save(p)
    assert imgconv.readable_copy(p, tmp_path) == (None, "")


def test_tiff_becomes_a_smaller_rgb_jpeg_next_to_the_original(tmp_path):
    src = _tiff(tmp_path / "scan.TIF")
    out, note = imgconv.readable_copy(src, tmp_path)
    assert out == tmp_path / "scan.TIF.jpg" and out.exists()
    assert "已自动转成" in note and "scan.TIF.jpg" in note
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.mode == "RGB"
        assert max(im.size) == imgconv.MAX_SIDE          # 长边缩到上限,比例不变
    assert not (tmp_path / "scan.TIF.jpg.tmp").exists()


def test_exif_orientation_is_applied(tmp_path):
    """手机照片的方向在 EXIF 里;不转正,模型看到的是横着的字。"""
    src = _tiff(tmp_path / "rot.tif", size=(400, 100), orientation=6)   # 6 = 需要顺时针转 90 度
    out, _ = imgconv.readable_copy(src, tmp_path)
    with Image.open(out) as im:
        assert im.size == (100, 400)


def test_without_pillow_the_user_is_asked_to_convert(tmp_path, monkeypatch):
    src = _tiff(tmp_path / "x.tif", size=(10, 10))
    monkeypatch.setitem(sys.modules, "PIL", None)
    out, note = imgconv.readable_copy(src, tmp_path)
    assert out is None and "pillow" in note and "jpg / png" in note


def test_heic_without_pillow_heif_says_which_component_is_missing(tmp_path, monkeypatch):
    src = tmp_path / "IMG_1.HEIC"
    src.write_bytes(b"\x00\x00\x00 ftypheic")
    monkeypatch.setitem(sys.modules, "pillow_heif", None)
    out, note = imgconv.readable_copy(src, tmp_path)
    assert out is None and "HEIC" in note and "pillow-heif" in note and "jpg / png" in note


def test_a_broken_file_is_reported_not_raised(tmp_path):
    src = tmp_path / "bad.tif"
    src.write_bytes(b"not an image")
    out, note = imgconv.readable_copy(src, tmp_path)
    assert out is None and "转换失败" in note and "jpg / png" in note
    assert not (tmp_path / "bad.tif.jpg").exists()


def test_scan_points_the_work_order_at_the_converted_copy(tmp_path, capsys):
    import kb_init
    src = tmp_path / "in"
    src.mkdir()
    _tiff(src / "page1.tif", size=(800, 600))
    Image.new("RGB", (10, 10)).save(src / "page2.png")
    kb = tmp_path / "kb"
    rc = kb_init.main(["scan", str(src), "--kb", str(kb), "--batch", "img"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "TIF 已自动转成 page1.tif.jpg" in out
    work = {json.loads(p.read_text(encoding="utf-8"))["source_file"]: json.loads(p.read_text(encoding="utf-8"))
            for p in (kb / "init" / "img" / "work").glob("*.json")}
    assert work["page1.tif"]["image_path"].endswith("page1.tif.jpg")
    assert work["page2.png"]["image_path"].endswith("page2.png")
    assert pathlib.Path(work["page1.tif"]["image_path"]).exists()
    manifest = json.loads((kb / "init" / "img" / "manifest.json").read_text(encoding="utf-8"))
    assert [f.get("readable") for f in manifest["files"] if f["name"] == "page1.tif"] == ["sources/page1.tif.jpg"]
