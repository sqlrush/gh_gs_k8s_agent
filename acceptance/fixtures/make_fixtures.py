#!/usr/bin/env python3
"""生成验收题要用、但不适合放进仓库的二进制夹具(跑题脚本在每轮开始时调用)。

    python3 acceptance/fixtures/make_fixtures.py --out <目录> [--font <中文字体文件>]

目前只有一个:K3-scan.pdf —— 把 K3-scan.txt 渲染成**只有图片**的 PDF(像扫描件:没有文字层,略带倾斜与噪点),
〔〔 〕〕 括住的字做成模糊的。随机数种子固定,同一字体每次生成的文件一样。

要一个带中文字形的字体:不给 --font 就按常见位置找(macOS 苹方 / 黑体,Linux Noto CJK / 文泉驿),找不到报错退出 2。
依赖 Pillow(仓库测试依赖里已有)。
"""
from __future__ import annotations

import argparse
import pathlib
import random
import re
import sys
from typing import List, Optional, Sequence, Tuple

HERE = pathlib.Path(__file__).resolve().parent
SOURCE = HERE / "K3-scan.txt"
FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)
PAGE_W, PAGE_H, MARGIN = 1240, 1754, 110          # A4 · 150 dpi
FONT_PX, LINE_GAP, SEED = 30, 18, 20261027
_BLUR = re.compile(r"〔〔(.*?)〕〕")

Segment = Tuple[str, bool]                           # (文字, 是否模糊)


def parse(text: str) -> List[List[Segment]]:
    """原文 → 行;每行是若干段,模糊的段单独标出。# 开头的行是说明,不渲染。"""
    lines: List[List[Segment]] = []
    for raw in text.splitlines():
        if raw.startswith("#"):
            continue
        segs: List[Segment] = []
        pos = 0
        for m in _BLUR.finditer(raw):
            if m.start() > pos:
                segs.append((raw[pos:m.start()], False))
            segs.append((m.group(1), True))
            pos = m.end()
        if pos < len(raw):
            segs.append((raw[pos:], False))
        lines.append(segs)
    return lines


def plain(text: str) -> str:
    """转录应得到的文字(模糊处换成「[看不清]」),供评分对照。"""
    out = [_BLUR.sub("[看不清]", line) for line in text.splitlines() if not line.startswith("#")]
    return "\n".join(out).strip() + "\n"


def find_font(explicit: Optional[str]) -> pathlib.Path:
    for cand in ([explicit] if explicit else list(FONT_CANDIDATES)):
        p = pathlib.Path(cand)
        if p.is_file():
            return p
    raise FileNotFoundError("找不到带中文字形的字体,用 --font 指定一个 .ttf / .ttc / .otf 文件")


def _wrap(segs: Sequence[Segment], font, width: int) -> List[List[Segment]]:
    """按像素宽度折行(中文逐字折),模糊标记跟着字走。"""
    rows: List[List[Segment]] = [[]]
    used = 0.0
    for text, blurred in segs:
        for ch in text:
            w = font.getlength(ch)
            if used + w > width and rows[-1]:
                rows.append([])
                used = 0.0
            row = rows[-1]
            if row and row[-1][1] == blurred:
                row[-1] = (row[-1][0] + ch, blurred)
            else:
                row.append((ch, blurred))
            used += w
    return rows


def render_scan(lines: List[List[Segment]], font_path: pathlib.Path, out: pathlib.Path) -> int:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rnd = random.Random(SEED)
    font = ImageFont.truetype(str(font_path), FONT_PX)
    pages, page = [], None
    y = PAGE_H

    def new_page():
        img = Image.new("L", (PAGE_W, PAGE_H), 248)
        pages.append(img)
        return img

    for segs in lines:
        rows = _wrap(segs, font, PAGE_W - 2 * MARGIN) if segs else [[]]
        for row in rows:
            if page is None or y + FONT_PX > PAGE_H - MARGIN:
                page, y = new_page(), MARGIN
            x = MARGIN
            draw = ImageDraw.Draw(page)
            for text, blurred in row:
                w = int(font.getlength(text))
                if blurred:                          # 单独画在小图上再糊掉,贴回去
                    patch = Image.new("L", (w + 12, FONT_PX + 12), 248)
                    ImageDraw.Draw(patch).text((6, 2), text, font=font, fill=40)
                    patch = patch.filter(ImageFilter.GaussianBlur(7))
                    page.paste(patch, (x - 6, y - 2))
                else:
                    draw.text((x, y), text, font=font, fill=30)
                x += w
            y += FONT_PX + LINE_GAP
        y += LINE_GAP // 2
    scans = []
    for img in pages:                                # 扫描感:轻微倾斜 + 噪点
        noisy = img.rotate(rnd.uniform(-0.8, 0.8), resample=Image.BICUBIC, fillcolor=248)
        px = noisy.load()
        for _ in range(PAGE_W * PAGE_H // 400):
            px[rnd.randrange(PAGE_W), rnd.randrange(PAGE_H)] = rnd.choice((90, 160, 210))
        scans.append(noisy.convert("RGB"))
    out.parent.mkdir(parents=True, exist_ok=True)
    scans[0].save(str(out), "PDF", resolution=150.0, save_all=True, append_images=scans[1:])
    return len(scans)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="生成验收题的二进制夹具")
    ap.add_argument("--out", required=True, help="输出目录(不要指到仓库里)")
    ap.add_argument("--font", default="", help="带中文字形的字体文件")
    args = ap.parse_args(argv)
    try:
        font = find_font(args.font or None)
    except FileNotFoundError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 2
    text = SOURCE.read_text(encoding="utf-8")
    out_dir = pathlib.Path(args.out)
    n = render_scan(parse(text), font, out_dir / "K3-scan.pdf")
    (out_dir / "K3-scan.expected.txt").write_text(plain(text), encoding="utf-8")
    print("✓ K3-scan.pdf(%d 页,字体 %s)与转录对照 K3-scan.expected.txt → %s" % (n, font.name, out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
