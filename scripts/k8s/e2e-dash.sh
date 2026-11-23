#!/usr/bin/env bash
# 四个大盘页面的验收:用 headless Chrome 打开,断言各页的区块非空、console 没有 JS 错误。
#
#   scripts/k8s/e2e-dash.sh                 对着本地预览站(自己起、自己停),数据是 dash-samples.py 的样例
#   BASE=http://…/dash scripts/k8s/e2e-dash.sh   对着别的站(注意 Chrome 加不了网关要的信任头,真集群只能验到页面文件)
#
# 为什么非得真浏览器:设计稿阶段 Top SQL 那页曾整页空白 —— 顶层 `const top` 撞了 window.top,
# 解析期就死;node 里没有这个全局,查不出来。只有真浏览器能抓这一类。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/../.." && pwd)
CH=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
[ -x "$CH" ] || { echo "找不到 Chrome:$CH(可用 CHROME=… 指定)" >&2; exit 2; }
PORT=${PORT:-8091}
if [ -z "${BASE:-}" ]; then
  D=$(mktemp -d /tmp/e2e-dash.XXXXXX)
  PREVIEW_DIR="$D" nohup "$HERE/scripts/dash-preview.sh" "$PORT" >"$D/preview.log" 2>&1 &
  PV=$!
  trap 'kill $PV 2>/dev/null || true' EXIT
  sleep 3
  BASE="http://127.0.0.1:$PORT/dash"
fi

# 不用关联数组:macOS 自带的 bash 是 3.2,declare -A 直接报 unbound variable
ids_for() {
  case "$1" in
    health) echo "hd-head hd-ring hd-band hd-sub hd-findings hd-dims hd-history hd-trend" ;;
    topsql) echo "ts-tabs ts-kpis ts-stack ts-rows ts-cards" ;;
    wdr)    echo "wd-head wd-kpis wd-dbt wd-verdict wd-tsql wd-waits wd-small wd-history" ;;
    kb)     echo "kb-head kb-top kb-stores kb-health kb-misses kb-queries" ;;
  esac
}
fail=0
for p in health topsql wdr kb; do
  url="$BASE/$p/"
  ids=$(ids_for "$p")
  dom=$("$CH" --headless=new --disable-gpu --virtual-time-budget=6000 --dump-dom "$url" 2>/dev/null || true)
  errs=$("$CH" --headless=new --disable-gpu --virtual-time-budget=6000 --enable-logging=stderr --v=0 --dump-dom "$url" 2>&1 >/dev/null \
         | grep -iE "uncaught|typeerror|referenceerror|syntaxerror" || true)
  missing=""
  for id in $ids; do
    # 区块必须存在且里面有内容(不是空标签)
    if ! printf '%s' "$dom" | python3 -c '
import re,sys
d=sys.stdin.read(); i=sys.argv[1]
m=re.search(r"id=\"%s\"[^>]*>(.*?)</(div|svg|section|table|tbody)>" % i, d, re.S)
sys.exit(0 if m and m.group(1).strip() else 1)' "$id"; then
      missing="$missing $id"
    fi
  done
  if [ -n "$missing" ] || [ -n "$errs" ]; then
    fail=1
    echo "✗ $p  缺/空区块:[${missing# }]  JS 错误:${errs:-无}"
  else
    echo "✓ $p  $(echo "$ids" | tr ' ' ',') 全部非空,console 零错误"
  fi
done
[ "$fail" = 0 ] && echo "ALL OK" || { echo "有页面没过" >&2; exit 1; }
