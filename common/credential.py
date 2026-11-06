from __future__ import annotations

import base64
import os
import pathlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#from .config import _NAME_RE, ensure_dir, state_dir
try:
    from .config import _NAME_RE, ensure_dir, state_dir
except ImportError:
    from config import _NAME_RE, ensure_dir, state_dir

_KEY_SIZE = 32  # AES-256
_NONCE_SIZE = 12  # GCM standard nonce


class CredentialError(Exception):
    """Raised on missing/corrupted credentials or invalid names."""


def _key_path() -> pathlib.Path:
    return state_dir() / "key"


def _load_key() -> bytes:
    """Read the machine-local key, generating one atomically on first use."""
    path = _key_path()
    if path.exists():
        key = path.read_bytes()
        if len(key) != _KEY_SIZE:
            raise CredentialError(
                f"key {path}: want {_KEY_SIZE} bytes, got {len(key)}"
            )
        return key

    ensure_dir()
    fresh = os.urandom(_KEY_SIZE)
    # O_EXCL makes creation atomic: exactly one concurrent caller wins.
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process won the race; use the winner's key.
        return _load_key()
    try:
        os.write(fd, fresh)
    finally:
        os.close(fd)
    return fresh


def load_secret(name: str) -> str:
    """Return the decrypted credential for a connection name."""
    if not name or not _NAME_RE.match(name):
        raise CredentialError(f"invalid credential name {name!r}")

    env = os.environ.get("GSDB_PASSWORD") or os.environ.get("GDAA_PASSWORD")
    if env:
        return env

    key = _load_key()
    path = state_dir() / "credentials" / f"{name}.enc"
    try:
        sealed = path.read_bytes()
    except FileNotFoundError as exc:
        raise CredentialError(
            f"no stored credential for {name!r}: run `connect add {name} ...` first"
        ) from exc

    if len(sealed) < _NONCE_SIZE + 16:  # nonce + GCM tag
        raise CredentialError(f"credential {path}: corrupted (too short)")

    nonce, ciphertext = sealed[:_NONCE_SIZE], sealed[_NONCE_SIZE:]
    try:
        # AAD == name, matching Go's gcm.Seal(..., []byte(name)).
        plain = AESGCM(key).decrypt(nonce, ciphertext, name.encode())
    except Exception as exc:  # cryptography raises InvalidTag etc.
        raise CredentialError(f"decrypt credential {path}: {exc}") from exc
    return plain.decode()


def seal_secret(name: str, secret: str) -> str:
    """加密成可以内联进 config.yaml 的 base64 密文（AAD 用连接名）。

    与 credentials/<name>.enc 用同一把钥匙、同一个 AAD，两种放法可以互换。
    AAD 绑定连接名意味着：把 app1/conn1 的密文复制到 app2/conn1 底下会解不开
    —— 这是有意的，否则密文一旦泄露就能被挪到别的连接上复用。
    """
    if not name or not _NAME_RE.match(name):
        raise CredentialError(f"invalid credential name {name!r}")
    key = _load_key()
    nonce = os.urandom(_NONCE_SIZE)
    sealed = nonce + AESGCM(key).encrypt(nonce, secret.encode(), name.encode())
    return base64.b64encode(sealed).decode("ascii")


def open_secret(name: str, blob: str) -> str:
    """解开内联密文。"""
    if not name or not _NAME_RE.match(name):
        raise CredentialError(f"invalid credential name {name!r}")
    try:
        sealed = base64.b64decode(blob, validate=True)
    except Exception as exc:
        raise CredentialError(
            "连接 %r 的 password 标了 encrypted: true，但不是合法的 base64：%s\n"
            "明文口令请把 encrypted 设为 false（或删掉这一行）。" % (name, exc)
        ) from exc
    if len(sealed) < _NONCE_SIZE + 16:
        raise CredentialError(f"连接 {name!r} 的内联密文过短，已损坏")
    nonce, ciphertext = sealed[:_NONCE_SIZE], sealed[_NONCE_SIZE:]
    try:
        key = _load_key()
        return AESGCM(key).decrypt(nonce, ciphertext, name.encode()).decode()
    except Exception as exc:
        raise CredentialError(
            "解开连接 %r 的内联密文失败：%s\n"
            "密文与连接名绑定（AAD），改过 name 之后旧密文就解不开了 —— "
            "此时要用新名字重新加密，而不是改回去。" % (name, exc)
        ) from exc


def secret_for(conn) -> str:
    """取一条连接的口令。三个来源，优先级从高到低：

      1. 环境变量 GSDB_PASSWORD / GDAA_PASSWORD（临时覆盖，调试用）
      2. 配置里的内联 password（新格式；encrypted: true 时是密文）
      3. credentials/<name>.enc（旧格式，不动老配置也能继续用）

    driver=grmp 不走这里 —— 中间件用的是 token，不是数据库口令。
    """
    env = os.environ.get("GSDB_PASSWORD") or os.environ.get("GDAA_PASSWORD")
    if env:
        return env
    blob = getattr(conn, "password", "") or ""
    if blob:
        if getattr(conn, "encrypted", False):
            return open_secret(conn.name, blob)
        return blob
    return load_secret(conn.name)


def save_secret(name: str, secret: str) -> None:
    """Encrypt and store a credential (used by the connect skill)."""
    if not name or not _NAME_RE.match(name):
        raise CredentialError(f"invalid credential name {name!r}")

    key = _load_key()
    nonce = os.urandom(_NONCE_SIZE)
    sealed = nonce + AESGCM(key).encrypt(nonce, secret.encode(), name.encode())

    cred_dir = ensure_dir() / "credentials"
    cred_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cred_dir / f"{name}.enc"
    path.write_bytes(sealed)
    os.chmod(path, 0o600)
