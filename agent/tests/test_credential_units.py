import sys, pathlib
_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402
from common.credential import load_secret  # noqa: E402


def test_gsdb_password_used(tmp_path, monkeypatch):
    # Point the file store at an empty dir so only the env path can resolve.
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))
    monkeypatch.delenv("GDAA_PASSWORD", raising=False)
    monkeypatch.setenv("GSDB_PASSWORD", "newpw")
    assert load_secret("og-pri") == "newpw"

def test_gsdb_password_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))
    monkeypatch.setenv("GDAA_PASSWORD", "legacypw")
    monkeypatch.setenv("GSDB_PASSWORD", "newpw")
    assert load_secret("og-pri") == "newpw"

def test_legacy_gdaa_password_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("GDAA_HOME", str(tmp_path))
    monkeypatch.delenv("GSDB_PASSWORD", raising=False)
    monkeypatch.setenv("GDAA_PASSWORD", "legacypw")
    assert load_secret("og-pri") == "legacypw"
