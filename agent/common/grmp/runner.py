"""直连路径：不经中间件，直接连库执行已注册脚本。

**刻意与中间件路径产出相同的形状**（全字符串化的行字典），而不是返回
更丰富的原生类型。理由：skill 在客户环境拿到的一律是字符串，本地若能
拿到 int/bool，本地写出的解析代码到客户环境会全部失效。

直连路径的价值在于绕开白名单（本地想跑什么脚本就跑什么），不在于
拿到更好的数据。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from . import serialize
from .columns import check_columns
from .errors import QueryError
from .params import to_param_value
from .placeholder import render
from .registry import Registry
from .settings import Settings

DEFAULT_STATEMENT_TIMEOUT_SECONDS = 60


class RunError(QueryError):
    """执行层面的失败（无结果集等）。

    继承 QueryError，与中间件路径的 GrmpError 归一 ——
    skill 的降级逻辑只认 access.QueryError 一个类型。
    """


def _default_open_db(name: str, read_only: bool = True):
    from ..db import Database

    return Database.connect(name, read_only=read_only)


class DirectRunner:
    """driver 为 gsql/psycopg2 时使用。

    **不提供跨调用的持久会话**：每次 run() 开连接、执行完就 close。
    即便底层是支持持久会话的 psycopg2 也一样 —— 能力属于「这个入口」，
    不属于「这个驱动」。需要会话的流程（hypopg 虚拟索引验证等）
    要用 access.session_for_conn() 单独索取原始连接。
    """

    provides_session = False
    # 直连能跑任意 SQL —— 与 GrmpRunner 的差别就在这一位上。
    whitelist_only = False

    def __init__(
        self,
        conn_name: str,
        registry: Optional[Registry] = None,
        settings: Optional[Settings] = None,
        open_db: Optional[Callable[[str], Any]] = None,
        statement_timeout: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS,
    ):
        self.conn_name = conn_name
        self._registry = registry or Registry()
        self._settings = settings or Settings()
        self._open_db = open_db or _default_open_db
        self._timeout = statement_timeout

    def run(
        self, script_name: str, values: Mapping[str, Any] = None
    ) -> List[Dict[str, str]]:
        record = self._registry.find(script_name)
        raw = {k: to_param_value(v) for k, v in (values or {}).items()}

        # 渲染（含类型校验）在连库之前：参数不合法时根本不该建立连接
        sql = render(record.script_content, record.params, raw, self._settings)

        db = self._open_db(self.conn_name, record.readonly)
        try:
            db.set_statement_timeout(self._timeout)
            cols, rows = db.query(sql)
        except Exception as exc:
            # 归一到 QueryError：skill 的降级逻辑只认这一个类型，
            # 换访问方式时不用改 skill。原因用 from 链住，不吞掉。
            from .hints import with_hint   # 与中间件路径同一张「报错 → 提示」表
            raise QueryError(with_hint("执行脚本 %s 失败：%s" % (script_name, exc))) from exc
        finally:
            db.close()

        if not cols:
            # 不返回 []：那会让调用方把「这条语句没有结果集」读成「查到 0 行」
            raise RunError(
                "脚本 %s 未返回结果集。诊断脚本应当是查询语句。" % script_name
            )
        # 与中间件路径同一道检查：否则同一条坏脚本在直连下能跑、
        # 走中间件才炸，双路径一致性就成了摆设
        check_columns(cols, script_name)
        return serialize.result_array(cols, rows, self._settings)["data"]
