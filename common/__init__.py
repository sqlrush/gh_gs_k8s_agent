"""common — shared connection layer for opencode_skill.

Only connection/credential/driver plumbing lives here (the inherently shared
infrastructure every skill needs). Skill-specific logic and probes live under
each skill's scripts/ directory.
"""
from .config import Connection, ConfigError, find, load, validate
from .credential import CredentialError, load_secret, save_secret
from .db import Database, DBError

__all__ = [
    "Connection",
    "ConfigError",
    "find",
    "load",
    "validate",
    "CredentialError",
    "load_secret",
    "save_secret",
    "Database",
    "DBError",
]
