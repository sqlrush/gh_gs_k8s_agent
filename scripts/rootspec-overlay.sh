#!/usr/bin/env bash
# 把镜像里预留的三份根规范模板换成行内正式规范(行内部署时用)。在离线包上做,原包不动,另出一个新包目录:
#   runtime 与 kb-import 两个镜像各加一层,/workspace/docs/ 里的模板换成你给的文件;标签不变,其余镜像原样;
#   重新导出镜像包,用包里的发版台镜像刷新 release.yaml(镜像 digest、sha256)。新包手工部署(docker load)与发版台都能用。
#
# 用法: <离线包目录>/rootspec-overlay.sh --docs <放根规范的目录> [--package <离线包目录>] [--arch amd64|arm64] [--out <新包目录>]
#   --package 默认本脚本所在目录(脚本随离线包发出)
#   --docs   放正式规范的目录(.md / .txt / .docx / .pdf;Markdown 一级标题写规范名称,检索结果按它写出处)
#            pdf 在这里转成文字版 .txt 放进镜像(运行镜像里没有 pdftotext);扫描件没有文字层,要先转成 docx 或 md
#   --arch   默认本机架构;要和本机一致(加层时要在镜像里执行命令)
#   --out    默认 <离线包目录>-rootspec
# 要求:docker 25 及以上(导出的镜像包格式发版台才认得)。每次升级到新版本,用新版本的包再做一次。
set -euo pipefail
PKG=""; DOCS=""; ARCH=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --package) PKG=$2; shift 2 ;;
    --docs) DOCS=$2; shift 2 ;;
    --arch) ARCH=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "不认识的参数:$1(--help 看用法)" >&2; exit 2 ;;
  esac
done
SELF=$(cd "$(dirname "$0")" && pwd)
if [ -z "$PKG" ] && [ -f "$SELF/release.yaml" ]; then PKG=$SELF; fi
[ -n "$PKG" ] && [ -n "$DOCS" ] || { echo "要给 --docs;脚本不在离线包目录里时还要给 --package(--help 看用法)" >&2; exit 2; }
[ -f "$PKG/release.yaml" ] || { echo "$PKG 不是离线包目录(没有 release.yaml)" >&2; exit 1; }
[ -d "$DOCS" ] || { echo "根规范目录 $DOCS 不存在" >&2; exit 1; }
HOST=$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/'); ARCH=${ARCH:-$HOST}
[ "$ARCH" = "$HOST" ] || { echo "目标架构 $ARCH 与本机 $HOST 不同:请在 $ARCH 的机器上做" >&2; exit 1; }
command -v docker >/dev/null || { echo "这台机器上没有 docker" >&2; exit 1; }
DV=$(docker version --format '{{.Server.Version}}' 2>/dev/null | cut -d. -f1)
[ "${DV:-0}" -ge 25 ] 2>/dev/null || { echo "docker 版本要 25 及以上(现在 ${DV:-未知}):旧版导出的镜像包格式发版台不认" >&2; exit 1; }

# 规范文件:至少一份;不能还是模板;不能是空文件
FILES=()   # 不用 mapfile:跳板机上的 bash 可能是 3.x
while IFS= read -r f; do FILES+=("$f"); done < <(find "$DOCS" -maxdepth 1 -type f \( -iname '*.md' -o -iname '*.markdown' -o -iname '*.txt' -o -iname '*.docx' -o -iname '*.pdf' \) | sort)
[ "${#FILES[@]}" -ge 1 ] || { echo "$DOCS 里没有规范文件(.md / .txt / .docx / .pdf)" >&2; exit 1; }
for f in "${FILES[@]}"; do
  [ -s "$f" ] || { echo "空文件:$f" >&2; exit 1; }
  if grep -q "ROOTSPEC-TEMPLATE" "$f" 2>/dev/null; then echo "$f 还是模板(第一行有 ROOTSPEC-TEMPLATE):换成正式规范再来" >&2; exit 1; fi
done

TAG=$(sed -n 's/^tag: *//p' "$PKG/release.yaml" | tr -d "'\"" | head -1)
NAME="gaussdb-agent-${TAG#agent-}"; TAR="$PKG/$NAME-$ARCH.tar.gz"
RTAR=$(ls "$PKG"/gaussdb-agent-release-*-"$ARCH".tar.gz 2>/dev/null | head -1)
[ -f "$TAR" ] || { echo "包里没有 $ARCH 的镜像包 $(basename "$TAR")" >&2; exit 1; }
[ -n "$RTAR" ] || { echo "包里没有 $ARCH 的发版台镜像(刷新 release.yaml 要用)" >&2; exit 1; }
OUT=${OUT:-${PKG%/}-rootspec}
[ ! -e "$OUT" ] || { echo "$OUT 已存在:换个 --out 或先挪走" >&2; exit 1; }
(cd "$PKG" && { sha256sum -c "$(basename "$TAR").sha256" 2>/dev/null || shasum -a 256 -c "$(basename "$TAR").sha256"; } >/dev/null) \
  || { echo "$(basename "$TAR") 的 sha256 对不上:包拷坏了" >&2; exit 1; }

echo "== 1/4 导入 $(basename "$TAR") …"
IMGS=()
while IFS= read -r i; do IMGS+=("$i"); done < <(gunzip -c "$TAR" | docker load | sed -n 's/^Loaded image: //p')
[ "${#IMGS[@]}" -ge 2 ] || { echo "docker load 没给出镜像名" >&2; exit 1; }

echo "== 2/4 runtime 与 kb-import 加一层,换上 ${#FILES[@]} 份规范 …"
CTX=$(mktemp -d); trap 'rm -rf "$CTX"' EXIT
mkdir "$CTX/docs"; cp "${FILES[@]}" "$CTX/docs/"
KBI=""; for img in "${IMGS[@]}"; do case "$img" in *gaussdb-agent-kb-import:*) KBI=$img ;; esac; done
[ -n "$KBI" ] || { echo "镜像包里没有 gaussdb-agent-kb-import" >&2; exit 1; }
for f in "$CTX"/docs/*; do
  case "$f" in
    *.pdf|*.PDF)
      b=$(basename "$f"); t="${b%.*}.txt"
      [ ! -e "$CTX/docs/$t" ] || { echo "$b 与 $t 同名:改掉一个" >&2; exit 1; }
      docker run --rm --user "$(id -u):$(id -g)" -v "$CTX/docs:/d" --entrypoint pdftotext "$KBI" -layout "/d/$b" "/d/$t" \
        || { echo "$b 转文字失败" >&2; exit 1; }
      grep -q "[^[:space:]]" "$CTX/docs/$t" || { echo "$b 转不出文字(多半是扫描件):请先转成 docx 或 md" >&2; exit 1; }
      rm -f "$f"; echo "  $b → $t(文字版,镜像里检索读它)" ;;
  esac
done
cat > "$CTX/Dockerfile" <<'DF'
ARG BASE
FROM ${BASE}
USER root
RUN rm -f /workspace/docs/*
COPY docs/ /workspace/docs/
RUN chmod 0755 /workspace/docs && chmod 0644 /workspace/docs/* && python3 /workspace/search_docs.py --list
USER agent
DF
for img in "${IMGS[@]}"; do
  case "$img" in
    *gaussdb-agent-runtime:*|*gaussdb-agent-kb-import:*)
      docker build -q --platform "linux/$ARCH" --build-arg "BASE=$img" -t "$img" "$CTX" >/dev/null
      echo "  $img:$(docker run --rm --entrypoint python3 "$img" /workspace/search_docs.py --list | tr '\n' ' ')" ;;
  esac
done

echo "== 3/4 导出新包 $OUT …"
mkdir -p "$OUT"
for f in "$PKG"/*; do [ "$f" = "$TAR" ] || [ "$f" = "$TAR.sha256" ] || cp -p "$f" "$OUT/"; done
docker save "${IMGS[@]}" | gzip -6 > "$OUT/$(basename "$TAR")"

echo "== 4/4 刷新 release.yaml …"
RIMG=$(gunzip -c "$RTAR" | docker load | sed -n 's/^Loaded image: //p' | tail -1)
NOTE="根规范:$(for f in "${FILES[@]}"; do printf '%s ' "$(basename "$f")"; done)"
docker run --rm --user "$(id -u):$(id -g)" -v "$(cd "$OUT" && pwd):/pkg" --entrypoint python3 "$RIMG" -m release.repack /pkg --arch "$ARCH" --note "$NOTE"
echo
echo "完成:$OUT"
echo "  手工部署:用新包的 $(basename "$TAR")(docker load 后按部署手册推进内网仓库)"
echo "  用发版台:把新包目录放进发布包目录,并把原包目录挪走(同一版本只能有一份)"
