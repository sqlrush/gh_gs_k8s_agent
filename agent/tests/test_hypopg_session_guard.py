"""hypopg verify_indexes 必须拒绝无持久会话的后端(gsql)。

gsql 每条语句起独立子进程,SET enable_hypo_index / hypopg 虚拟索引不跨语句
留存,索引验证会静默失效——守卫改为在 verify_indexes 入口明确报错。
"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from common.backends.base import DBError  # noqa: E402
from common.backends.psycopg2_backend import Psycopg2Backend  # noqa: E402
from common.backends.gsql_backend import GsqlBackend  # noqa: E402
import common.db as dbmod  # noqa: E402


def _load_hypoindex(skill: str):
    path = _ROOT / "skills" / skill / "scripts" / "hypoindex.py"
    sys.path.insert(0, str(path.parent))  # sibling `cost`
    sys.path.insert(0, str(_ROOT))        # `common`
    spec = importlib.util.spec_from_file_location(f"{skill}_hypoindex", path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


class _SessionlessDB:
    provides_session = False


def test_backend_provides_session_flags():
    assert Psycopg2Backend.provides_session is True
    assert GsqlBackend.provides_session is False


def test_database_delegates_provides_session():
    class _B:
        provides_session = False

    d = dbmod.Database(_B(), conn=None)
    assert d.provides_session is False


@pytest.mark.parametrize("skill", ["gaussdb-sqltune", "gaussdb-proctune"])
def test_verify_indexes_rejects_sessionless_backend(skill):
    hypo = _load_hypoindex(skill)
    with pytest.raises(DBError) as ei:
        hypo.verify_indexes(_SessionlessDB(), "SELECT 1 FROM t WHERE x = 1")
    assert "psycopg2" in str(ei.value)  # actionable guidance
