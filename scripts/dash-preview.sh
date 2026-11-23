#!/usr/bin/env bash
# 本地预览大盘:拼一个假站 —— /dash/ 是 frontend/ 的**拷贝**,/reports/ 是样例报告(dash-samples.py
# 生成,布局与用户 Pod 里 4097 端出的一致),用 python3 -m http.server 端出。
#
#   scripts/dash-preview.sh [端口]        默认 8090;Ctrl-C 结束
#   PREVIEW_DIR 环境变量可指定拼站目录(默认临时目录);改了 frontend/ 之后重跑本脚本
#
# 为什么是拷贝不是软链:http.server 没有 nginx 的 try_files,/dash/health 这种路径要靠
# dash/health/index.html 才能打开,而那几个目录不该出现在源码目录里。
# 这不是网关:没有工号路由,所有人看到的都是样例;深挖会 404(没有 opencode)。只用于开发与页面验收。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
PORT=${1:-8090}
D=${PREVIEW_DIR:-$(mktemp -d /tmp/dash-preview.XXXXXX)}
mkdir -p "$D"
rm -rf "$D/dash" "$D/reports"
cp -R "$HERE/frontend" "$D/dash"
for p in health topsql wdr kb; do
  mkdir -p "$D/dash/$p"
  cp "$HERE/frontend/index.html" "$D/dash/$p/index.html"
done
python3 "$HERE/scripts/dash-samples.py" "$D/reports" >/dev/null
printf '<!DOCTYPE html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=/dash/health/">' > "$D/index.html"
echo "预览站目录:$D"
echo "打开:http://127.0.0.1:$PORT/dash/health/   (topsql / wdr / kb 同理)"
cd "$D" && exec python3 -m http.server "$PORT" --bind 127.0.0.1
