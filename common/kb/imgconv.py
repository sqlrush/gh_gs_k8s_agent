"""把 read 工具打不开的图片（HEIC / HEIF / TIFF）转成 JPEG 副本，给模型看图用。

opencode 的 read 工具只认 png / jpg / gif / webp 这几种；苹果手机直出的 HEIC 是现场最常见的照片格式。
2026-10-22 用户反馈：一批 HEIC 照片一张都没读出来，模型让用户「转成 jpg / png 再传」。

依赖 pillow（TIFF）与 pillow-heif（HEIC / HEIF），**只装在知识库导入镜像里**（诊断镜像不需要看图）。
没装时返回 None 和原因，调用方照旧请用户自己转格式——不抛异常，扫描不能因为一张图中断。
"""
from __future__ import annotations

import os
import pathlib
from typing import Optional, Tuple

CONVERTIBLE = frozenset({".heic", ".heif", ".tif", ".tiff"})
_HEIF = frozenset({".heic", ".heif"})
# 长边上限：手机原图 4032 像素，工单 / 报告类照片缩到 2560 文字仍清楚，图片体积小一半以上
MAX_SIDE = 2560
JPEG_QUALITY = 90


def _ask_user(fmt: str) -> str:
    return "请用户把这张 %s 转成 jpg / png 再传" % fmt


def readable_copy(src: pathlib.Path, dest_dir: pathlib.Path) -> Tuple[Optional[pathlib.Path], str]:
    """(转换后的 JPEG 路径, 说明)。不需要转的格式返回 (None, "")；转不了返回 (None, 原因)。

    副本名是「原文件名.jpg」（IMG_2357.HEIC.jpg），和原件放在一起，一眼能看出是哪张转来的。
    """
    suffix = src.suffix.lower()
    if suffix not in CONVERTIBLE:
        return None, ""
    fmt = suffix.lstrip(".").upper()
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None, "%s：环境里没有图片转换组件（pillow），%s" % (fmt, _ask_user(fmt))
    if suffix in _HEIF:
        try:
            import pillow_heif
        except ImportError:
            return None, "%s：环境里没有 HEIC 解码组件（pillow-heif），%s" % (fmt, _ask_user(fmt))
        pillow_heif.register_heif_opener()

    dest = dest_dir / (src.name + ".jpg")
    tmp = dest_dir / (src.name + ".jpg.tmp")
    try:
        with Image.open(src) as im:
            # 手机照片的方向记在 EXIF 里，不按它转正，模型看到的是横着的字
            out = ImageOps.exif_transpose(im).convert("RGB")
            out.thumbnail((MAX_SIDE, MAX_SIDE))
            out.save(tmp, "JPEG", quality=JPEG_QUALITY)
        os.replace(tmp, dest)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        tmp.unlink(missing_ok=True)
        return None, "%s 转换失败（%s：%s），%s" % (fmt, type(exc).__name__, exc, _ask_user(fmt))
    return dest, "%s 已自动转成 %s（供 read 看图）" % (fmt, dest.name)
