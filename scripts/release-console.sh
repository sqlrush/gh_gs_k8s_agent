#!/usr/bin/env bash
# 发版台一条命令起(随离线包发出,在包目录里)。跑在跳板机上,要 docker;不进 k8s。
#
# 用法:把整个包目录拷进「发布包目录」(如 /data/releases/gaussdb-agent-v1.4.4-oc1.18.27/),然后
#   <包目录>/release-console.sh                 起(已在跑同一版本就只打印地址;换版本会先确认没有发布正在执行)
#   <包目录>/release-console.sh stop            停
#   <包目录>/release-console.sh logs            看日志
# 选项(都有默认值):
#   --releases <目录>    发布包目录,默认 = 本包目录的上一级
#   --state <目录>       发版台状态(SQLite、渲染出的清单、日志),默认 ~/gaussdb-release-state;换版本时沿用同一个
#   --kubeconfig <文件>  默认 $KUBECONFIG 或 ~/.kube/config
#   --port <端口>        默认 8090,只听 127.0.0.1(不做登录);别的机器上看用 ssh -L 8090:127.0.0.1:8090 <跳板机>
#   --kubectl <文件>     换成集群配套的 kubectl(静态二进制);镜像自带的版本与集群差得多时资源扫描会提示
#   --ca <文件>          内网镜像仓库的 CA 证书,资源配置里「CA 证书」填同一路径
#   --force              有发布正在执行也替换(那一步会被中断,重启后停在原处等续跑)
#
# 目录与文件按**宿主机上的原路径**挂进容器,界面和日志里看到的路径就是跳板机上的路径。
# 用宿主机网络:kubeconfig 与镜像仓库地址里的 127.0.0.1(ssh 隧道)在容器里照样通。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=gaussdb-release
CMD=start
RELEASES=$(dirname "$HERE"); STATE="$HOME/gaussdb-release-state"; KCFG="${KUBECONFIG:-$HOME/.kube/config}"; KCFG=${KCFG%%:*}
PORT=8090; KUBECTL=""; CA=""; FORCE=""
while [ $# -gt 0 ]; do
  case "$1" in
    start|stop|logs) CMD=$1; shift ;;
    --releases) RELEASES=$2; shift 2 ;;
    --state) STATE=$2; shift 2 ;;
    --kubeconfig) KCFG=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --kubectl) KUBECTL=$2; shift 2 ;;
    --ca) CA=$2; shift 2 ;;
    --force) FORCE=yes; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "不认识的参数:$1(--help 看用法)" >&2; exit 2 ;;
  esac
done
command -v docker >/dev/null || { echo "这台机器上没有 docker:发版台以容器方式运行" >&2; exit 1; }
abspath() { (cd "$(dirname "$1")" && echo "$(pwd)/$(basename "$1")"); }

case "$CMD" in
  stop) docker rm -f "$NAME" >/dev/null 2>&1 && echo "已停" || echo "没在跑"; exit 0 ;;
  logs) exec docker logs --tail 200 -f "$NAME" ;;
esac

# 本机架构对应的发版台镜像包
case "$(uname -m)" in x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) echo "不支持的架构 $(uname -m)" >&2; exit 1 ;; esac
TAR=$(ls "$HERE"/gaussdb-agent-release-*-"$ARCH".tar.gz 2>/dev/null | head -1)
[ -n "$TAR" ] || { echo "包目录里没有本机架构($ARCH)的发版台镜像 gaussdb-agent-release-*-$ARCH.tar.gz" >&2; exit 1; }
[ -f "$TAR.sha256" ] || { echo "缺 $(basename "$TAR").sha256,不知道包是否完整" >&2; exit 1; }
(cd "$HERE" && { sha256sum -c "$(basename "$TAR").sha256" 2>/dev/null || shasum -a 256 -c "$(basename "$TAR").sha256"; } >/dev/null) \
  || { echo "$(basename "$TAR") 的 sha256 对不上:包拷坏了,重新拷" >&2; exit 1; }

# 各路径:必须存在;都转成绝对路径(挂载要求)
[ -d "$RELEASES" ] || { echo "发布包目录 $RELEASES 不存在" >&2; exit 1; }
[ -f "$KCFG" ] || { echo "kubeconfig $KCFG 不存在(--kubeconfig 指定)" >&2; exit 1; }
[ -z "$KUBECTL" ] || [ -f "$KUBECTL" ] || { echo "--kubectl $KUBECTL 不存在" >&2; exit 1; }
[ -z "$CA" ] || [ -f "$CA" ] || { echo "--ca $CA 不存在" >&2; exit 1; }
mkdir -p "$STATE"; chmod 700 "$STATE"
RELEASES=$(cd "$RELEASES" && pwd); STATE=$(cd "$STATE" && pwd); KCFG=$(abspath "$KCFG")
KDIR=$(dirname "$KCFG")
MOUNTS=(-v "$RELEASES:$RELEASES:ro" -v "$STATE:$STATE" -v "$KDIR:$KDIR:ro")
# kubeconfig 引用的证书 / 令牌文件不在同一目录时也挂进去
while read -r f; do
  [ -f "$f" ] || continue
  case "$f" in "$KDIR"/*) ;; *) MOUNTS+=(-v "$f:$f:ro") ;; esac
done < <(grep -E '^[[:space:]]*(certificate-authority|client-certificate|client-key|tokenFile):[[:space:]]*/' "$KCFG" \
         | sed -E 's/^[^:]+:[[:space:]]*//; s/["'"'"']//g; s/[[:space:]]+$//')
if grep -qE '^[[:space:]]*exec:' "$KCFG"; then
  echo "注意:kubeconfig 用了 exec 认证插件,容器里没有那个程序 —— 资源扫描「Kubernetes」会失败。换成令牌 / 证书方式的 kubeconfig。" >&2
fi
[ -z "$KUBECTL" ] || MOUNTS+=(-v "$(abspath "$KUBECTL"):/usr/local/bin/kubectl:ro")
[ -z "$CA" ] || { CA=$(abspath "$CA"); MOUNTS+=(-v "$CA:$CA:ro"); }
SEC=()
if command -v getenforce >/dev/null && [ "$(getenforce)" = "Enforcing" ]; then
  SEC=(--security-opt label=disable)          # SELinux:不给宿主机文件重打标签,只对这个容器关掉隔离标签
fi

echo "导入发版台镜像 $(basename "$TAR") …"
IMAGE=$(gunzip -c "$TAR" | docker load | sed -n 's/^Loaded image: //p' | tail -1)
[ -n "$IMAGE" ] || { echo "docker load 没有给出镜像名" >&2; exit 1; }

# 已经在跑:同一镜像(按镜像 ID,同一标签重新打过包也算换了)只打印地址;换了先看有没有发布正在执行
if docker inspect "$NAME" >/dev/null 2>&1; then
  OLD=$(docker inspect -f '{{.Config.Image}}' "$NAME"); UP=$(docker inspect -f '{{.State.Running}}' "$NAME")
  if [ "$(docker inspect -f '{{.Image}}' "$NAME")" = "$(docker image inspect -f '{{.Id}}' "$IMAGE")" ] && [ "$UP" = "true" ]; then
    echo "发版台已在运行($IMAGE):http://127.0.0.1:$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME" | sed -n 's/^RELEASE_LISTEN=.*://p')/"
    exit 0
  fi
  if [ "$UP" = "true" ] && [ -z "$FORCE" ]; then
    BUSY=$(docker exec "$NAME" python3 -c "
import json, os, urllib.request
h, _, p = os.environ['RELEASE_LISTEN'].rpartition(':'); u = 'http://127.0.0.1:%s/api/' % p
a = json.load(urllib.request.urlopen(u + 'overview', timeout=10)).get('active')
print(a if a and json.load(urllib.request.urlopen(u + 'runs/%d' % a, timeout=10)).get('status') == 'running' else '')" 2>/dev/null || echo "?")
    if [ "$BUSY" = "?" ]; then echo "读不到旧发版台的状态,不替换;确认没有发布在执行后加 --force" >&2; exit 1; fi
    if [ -n "$BUSY" ]; then echo "发布 #$BUSY 正在执行,不替换(等它停在关卡或做完;非换不可加 --force)" >&2; exit 1; fi
  fi
  echo "替换旧发版台($OLD → $IMAGE),状态目录沿用 $STATE"
  docker rm -f "$NAME" >/dev/null
fi

docker run -d --name "$NAME" --restart unless-stopped --network host --user "$(id -u):$(id -g)" ${SEC[@]+"${SEC[@]}"} \
  -e RELEASE_LISTEN="127.0.0.1:$PORT" -e RELEASE_DIR="$RELEASES" -e RELEASE_STATE="$STATE" -e KUBECONFIG="$KCFG" \
  "${MOUNTS[@]}" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do
  if docker exec "$NAME" python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/api/overview', timeout=3)" 2>/dev/null; then
    echo "发版台已启动:http://127.0.0.1:$PORT/   (别的机器上看:ssh -L $PORT:127.0.0.1:$PORT <这台跳板机>)"
    echo "  发布包目录 $RELEASES · 状态 $STATE · kubeconfig $KCFG"
    exit 0
  fi
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = "true" ] || break
  sleep 1
done
echo "发版台没起来,最后的日志:" >&2; docker logs --tail 30 "$NAME" >&2; exit 1
