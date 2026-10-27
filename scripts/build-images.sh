#!/usr/bin/env bash
# 构建后端三个镜像;第 4 个 kbstd(标准知识库:GaussDB 官方文档集)要给 --kbstd <放 kb-std-*.tar.gz 的目录> 才构建
# (包不在代码仓库里,由我方随版本发布在镜像仓与离线包里),不给就跳过并说明。默认本机架构;--platform linux/amd64,linux/arm64 交叉构建(需要 buildx)。
#
# 用法: scripts/build-images.sh [TAG] [--platform P] [--registry R --push [--single-arch]] [--kbstd <目录>]
#   本地构建:   scripts/build-images.sh dev
#   推仓库:     scripts/build-images.sh agent-v1.0.5-oc1.18.27 \
#                 --registry ghcr.io/sqlrush --platform linux/amd64,linux/arm64 --push
#
# **--push 有两道闸,都是 2026-09-22 客户拉不到镜像换来的。** 当时镜像是在 Mac
# (aarch64)上 `docker tag 本地镜像 → docker push` 推到 GHCR 的,客户在 x86 服务器上拉:
#   no matching manifest for linux/amd64/v3 in the manifest list entries
# 标签在、权限也对,那个 manifest list 里**只有 arm64**。而推送侧全程没有任何报错:
# push 成功、在 Mac 上 pull 回来也正常 —— 只有客户那台机器会失败。
#   ① --push 必须带 --registry:没有仓库前缀的 tag 推到 docker.io/library,说不清推去了哪;
#   ② --push 必须是多架构,除非显式 --single-arch 认下「这次只推本机架构」。
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
TAG=""
if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then TAG=$1; shift; fi
[ -n "$TAG" ] || TAG="$(git -C "$HERE" describe --tags --always)-oc1.18.27"
PLATFORM=""; PUSH=""; REGISTRY=""; SINGLE=""; KBSTD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM="--platform $2"; shift 2 ;;
    --registry) REGISTRY="${2%/}"; shift 2 ;;
    --push) PUSH="--push"; shift ;;
    --single-arch) SINGLE="yes"; shift ;;
    --kbstd) KBSTD="$2"; shift 2 ;;
    *) echo "unknown $1" >&2; exit 2 ;;
  esac
done

if [ -n "$KBSTD" ] && ! ls "$KBSTD"/kb-std-*.tar.gz >/dev/null 2>&1; then
  echo "--kbstd $KBSTD 里没有标准知识库包(kb-std-*.tar.gz);包由 kb.py docset pack 打出" >&2
  exit 2
fi

if [ -n "$PUSH" ]; then
  if [ -z "$REGISTRY" ]; then
    echo "--push 必须同时给 --registry <仓库前缀>(如 --registry ghcr.io/sqlrush)。" >&2
    echo "没有前缀的 tag 会被推去 docker.io/library —— 那不是我们的仓库。" >&2
    exit 2
  fi
  case "$PLATFORM" in
    *amd64*arm64*|*arm64*amd64*) ;;
    *)
      if [ -z "$SINGLE" ]; then
        echo "--push 只给了 ${PLATFORM:-本机架构},推上去就是**单架构镜像**。" >&2
        echo "客户在另一种架构上拉会看到 'no matching manifest for linux/... in the manifest" >&2
        echo "list entries',而推送这一侧不会有任何报错。两条路选一条:" >&2
        echo "  加 --platform linux/amd64,linux/arm64  (推荐,一个标签同时含两种架构)" >&2
        echo "  加 --single-arch                       (明确只推本机架构)" >&2
        exit 2
      fi ;;
  esac
fi

PREFIX=""; [ -n "$REGISTRY" ] && PREFIX="$REGISTRY/"
LOAD=""; [ -z "$PLATFORM" ] && [ -z "$PUSH" ] && LOAD="--load"
for T in runtime kb-import; do
  # shellcheck disable=SC2086
  docker buildx build $PLATFORM $LOAD $PUSH -f "$HERE/docker/Dockerfile" --target "$T" \
    -t "${PREFIX}gaussdb-agent-$T:$TAG" "$HERE"
done
# 网关是独立 Dockerfile:它不带 skill、不装 psycopg2、也不要 opencode 二进制,
# 跟两个用户镜像没有可复用的层,放一起只会让用户镜像多背 100 MB。
# shellcheck disable=SC2086
docker buildx build $PLATFORM $LOAD $PUSH -f "$HERE/docker/Dockerfile.gateway" \
  -t "${PREFIX}gaussdb-agent-gateway:$TAG" "$HERE"
# 前端不在本仓库构建:由前端方自建镜像,按 docs/前端对接接口说明.md 接入
echo "built: ${PREFIX}gaussdb-agent-{runtime,kb-import,gateway}:$TAG"
# 标准知识库(官方文档集):只装数据包,部署时由 Job kbstd-install 装进 NAS。包不在代码仓库里,要从 --kbstd 传进来
if [ -n "$KBSTD" ]; then
  # shellcheck disable=SC2086
  docker buildx build $PLATFORM $LOAD $PUSH -f "$HERE/docker/Dockerfile.kbstd" --build-context "kbstd=$KBSTD" \
    -t "${PREFIX}gaussdb-agent-kbstd:$TAG" "$HERE"
  echo "built: ${PREFIX}gaussdb-agent-kbstd:$TAG($(cd "$KBSTD" && ls kb-std-*.tar.gz | tr '\n' ' '))"
else
  echo "没构建 gaussdb-agent-kbstd(标准知识库):没给 --kbstd <放 kb-std-*.tar.gz 的目录>。它由我方随版本发布,从镜像仓或离线包取"
fi

if [ -n "$PUSH" ]; then
  echo
  echo "推完请**在另一种架构上验一次**,或者查清单里有没有两个架构:"
  echo "  docker buildx imagetools inspect ${PREFIX}gaussdb-agent-runtime:$TAG"
  echo "期望看到 linux/amd64 与 linux/arm64 两行。"
fi
