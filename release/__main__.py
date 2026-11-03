"""发版台入口:`python3 -m release`。

环境变量(镜像里有默认值,一般不用改):
  RELEASE_LISTEN   监听地址,默认 127.0.0.1:8090(容器里设 0.0.0.0:8090,由 docker -p 127.0.0.1:8090:8090 只暴露给本机)
  RELEASE_DIR      发布包目录,默认 /releases
  RELEASE_STATE    状态目录,默认 /state(SQLite、渲染出的清单、变更预览)
  KUBECONFIG       kubeconfig 路径,默认 /kube/config
"""
from __future__ import annotations

import os
import pathlib
import sys

from .app import App
from .server import serve
from .store import Store


def main() -> int:
    host, _, port = (os.environ.get("RELEASE_LISTEN") or "127.0.0.1:8090").rpartition(":")
    state = pathlib.Path(os.environ.get("RELEASE_STATE") or "/state")
    store = Store(state / "release.db")
    interrupted = store.mark_interrupted()
    if interrupted:
        print("上次正在执行的发布被中断:#%s,已停在原处,请在界面上选续跑或回滚" % ", #".join(map(str, interrupted)), flush=True)
    app = App(store, pathlib.Path(os.environ.get("RELEASE_DIR") or "/releases"), state,
              kubeconfig=os.environ.get("KUBECONFIG") or "")
    waiting = app.reattach()
    if waiting:
        print("接回停在关卡上的发布:#%s" % ", #".join(map(str, waiting)), flush=True)
    srv = serve(app, host or "127.0.0.1", int(port or 8090), ready=lambda m: print(m, flush=True))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
