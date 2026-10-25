#!/usr/bin/env python3
"""把 0.0.0.0:<listen> 转发到 127.0.0.1:<target>。

GRMP mock 刻意只绑 127.0.0.1;冒烟时容器要经 host.docker.internal 访问它,需要一个绑所有地址的入口。
只用于本机冒烟,不进镜像。用法: tcp-forward.py 8781 8779
"""
import socket
import sys
import threading


def pump(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


def main() -> int:
    listen, target = int(sys.argv[1]), int(sys.argv[2])
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen))
    srv.listen(16)
    while True:
        c, _ = srv.accept()
        try:
            t = socket.create_connection(("127.0.0.1", target), timeout=10)
        except OSError:
            c.close()
            continue
        threading.Thread(target=pump, args=(c, t), daemon=True).start()
        threading.Thread(target=pump, args=(t, c), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
