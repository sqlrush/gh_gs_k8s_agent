#!/bin/bash
# 在与镜像同一个基础镜像(Linux)里跑单测:scripts/linux-pytest.sh docker/tests [pytest 参数…]
#
# 为什么要有它:开发机是 macOS,镜像是 Linux。文件系统行为并不总一样——2026-10-14 实测
# overlayfs 上对旧目录描述符第一次 scandir 返回空,macOS 上全绿的 fdtree 测试在这里红了 9 条。
# 凡是碰文件系统语义(描述符、链接、权限)的改动,发版前两边各跑一遍。
#
# 依赖装在 Docker 卷 linux-pytest-sp 里(第一次运行时装一次,之后复用):pytest + 镜像里的 Python 依赖。
# 依赖有变时改 READY 标记的名字,旧卷会自动重装(2026-10-23 加 sqlite-vec)。
# 不能借宿主 p2venv 的 site-packages——cryptography 是编译过的 macOS 二进制,Linux 里加载不了。
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMG=python:3.12-slim-bookworm
VOL=linux-pytest-sp
READY=.ready-sqlitevec
export PATH=$HOME/.orbstack/bin:/usr/local/bin:$PATH
if ! docker run --rm -v "$VOL:/sp" "$IMG" test -f "/sp/$READY"; then
  echo "首次运行:往卷 $VOL 装 pytest 与依赖 …" >&2
  docker run --rm -v "$VOL:/sp" "$IMG" sh -c \
    "pip install -q --no-cache-dir --target /sp --upgrade pytest 'psycopg2-binary==2.9.10' 'cryptography>=41' 'PyYAML>=6' 'sqlite-vec==0.1.9' && touch /sp/$READY"
fi
exec docker run --rm --user 1000:1000 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$ROOT:/w:ro" -v "$VOL:/sp:ro" -e PYTHONPATH=/sp -w /w \
  "$IMG" python -m pytest -q -p no:cacheprovider "$@"
