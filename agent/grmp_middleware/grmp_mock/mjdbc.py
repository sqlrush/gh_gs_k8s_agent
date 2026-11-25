"""模拟现场中间件连 M 兼容业务库时的取值形态(v1.5.2,2026-11-25 现场反馈)。

现场中间件是 Java 写的:按 JDBC 类型映射取出每一列的对象,再转成字符串放进响应。连到 M 兼容库时,
布尔列取出来是字节数组,转成字符串就成了 ``[B@276fe535`` 这种对象地址 —— 原值丢了。1.5.1 的 M 兼容
模拟测试是用 gsql 直接跑 SQL、看文本输出,经过不了这一层,所以没测出来。

``--m-jdbc`` 打开后,mock 连到兼容模式为 B / M 的库时按最坏情况渲染:输出列的类型不在
《GaussDB M-Compatibility 开发指南》JDBC 映射表里「取出来就是文字 / 整数 / 小数」的那几种
(``SAFE_TYPE_OIDS``)时,非空值一律变成 ``[B@xxxxxxx``。A 兼容库照旧。

``name`` 类型不在映射表里,但现场已证实它能正常取到(登录探针 login.current_db 的库名列就是 name 类型,
1.5.1 在现场 M 库上按库名核对切库成功),所以算安全;其余一概按不安全处理,宁可多报。
"""
from __future__ import annotations

import zlib
from typing import Any, Callable, Optional

# text、varchar、bpchar、int2、int4、int8、numeric、float4、float8,以及现场已证实的 name
SAFE_TYPE_OIDS = frozenset({25, 1043, 1042, 21, 23, 20, 1700, 700, 701, 19})
M_LIKE_COMPAT = frozenset({"B", "M"})
BOOL_OID = 16


def java_bytes_repr(value: Any) -> str:
    """Java 对 byte[] 调 toString() 的样子:[B@ 加十六进制的对象哈希。这里用值的 CRC 让结果稳定、便于比对。"""
    return "[B@%x" % (zlib.crc32(repr(value).encode("utf-8")) & 0x7FFFFFFF)


class MJdbcDatabase:
    """包住 common.db.Database:兼容模式为 B / M 时按上面的规则改写结果,其余方法原样转给里面那个连接。"""

    def __init__(self, inner: Any):
        self._inner = inner
        self._compat: Optional[str] = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _raw(self):
        return getattr(getattr(self._inner, "_backend", None), "_raw", None)

    def _is_m_like(self, raw) -> bool:
        if self._compat is None:
            cur = raw.cursor()
            cur.execute("SELECT datcompatibility::text FROM pg_database WHERE datname = current_database()")
            row = cur.fetchone()
            self._compat = str((row or [""])[0] or "").strip().upper()
        return self._compat in M_LIKE_COMPAT

    def query(self, sql, params=None):
        raw = self._raw()
        if raw is None or not self._is_m_like(raw):
            return self._inner.query(sql, params)
        import psycopg2.extensions as ext
        from common.backends.base import DBError
        from common.backends.psycopg2_backend import _format_pg_error

        cur = raw.cursor()
        # B 兼容库(dolphin)把布尔值按 0 / 1 发回来,psycopg2 默认的布尔解析器认不得;原样取字符串,下面再统一改写
        ext.register_type(ext.new_type((BOOL_OID,), "M_JDBC_BOOL_RAW", lambda v, _c: v), cur)
        try:
            cur.execute(sql, params)
            desc = cur.description or []
            rows = cur.fetchall() if desc else []
        except Exception as exc:  # noqa: BLE001 —— 与 psycopg2 后端同样的报错形态
            raise DBError(_format_pg_error(exc)) from exc
        cols = [d[0] for d in desc]
        unsafe = [i for i, d in enumerate(desc) if d[1] not in SAFE_TYPE_OIDS]
        if not unsafe:
            return cols, [tuple(r) for r in rows]
        out = []
        for r in rows:
            r = list(r)
            for i in unsafe:
                if r[i] is not None:
                    r[i] = java_bytes_repr(r[i])
            out.append(tuple(r))
        return cols, out


def wrap(open_db: Callable[..., Any]) -> Callable[..., Any]:
    """把 open_db 包一层:每个打开的连接都套上 MJdbcDatabase。"""
    def _open(*args, **kwargs):
        return MJdbcDatabase(open_db(*args, **kwargs))
    return _open
