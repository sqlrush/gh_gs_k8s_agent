"""backends — 连接层的可插拔驱动后端。

后端按需惰性导入（见 common/db.py 的 _load_backend），因此 gsql-only
环境无需安装 psycopg2，反之亦然。
"""
from .base import Backend, DBError

__all__ = ["Backend", "DBError"]
