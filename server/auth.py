"""
Single-user passcode + signed device tokens.

Flow:
  1. First boot — server has no passcode → /api/auth/status returns
     {needs_setup: true}. Client posts a new passcode to /api/auth/setup;
     we hash it (scrypt) and write ~/.agentic-rag/auth.json with the hash
     + a random server secret.
  2. Subsequent logins — client posts passcode to /api/auth/login; we
     verify against the stored hash and return a signed token
     (itsdangerous URLSafeSerializer) bound to the server secret. The
     token's hash is also stored in auth_tokens so it can be revoked.
  3. Each protected request sends `Authorization: Bearer <token>`. We
     verify the signature AND check the hash is still in the DB.

Single-user: log in on each device you want access from, revoke
individual tokens from the UI when needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from itsdangerous import BadSignature, URLSafeSerializer

from server import db


_AUTH_FILE = Path.home() / ".agentic-rag" / "auth.json"
_TOKEN_SALT = "agentic-rag.device-token.v1"

# scrypt cost — interactive (~100ms on a modern Mac), plenty for a passcode
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


@dataclass
class AuthConfig:
    passcode_hash: str   # hex
    passcode_salt: str   # hex
    server_secret: str   # hex — used by itsdangerous

    def to_json(self) -> str:
        return json.dumps({
            "passcode_hash": self.passcode_hash,
            "passcode_salt": self.passcode_salt,
            "server_secret": self.server_secret,
        }, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "AuthConfig":
        d = json.loads(raw)
        return cls(
            passcode_hash=d["passcode_hash"],
            passcode_salt=d["passcode_salt"],
            server_secret=d["server_secret"],
        )


# ---------------------------------------------------------------------------
# Passcode hashing
# ---------------------------------------------------------------------------

def _hash_passcode(passcode: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passcode.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


def _load_config() -> AuthConfig | None:
    if not _AUTH_FILE.exists():
        return None
    try:
        return AuthConfig.from_json(_AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _save_config(cfg: AuthConfig) -> None:
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_FILE.write_text(cfg.to_json(), encoding="utf-8")
    # Best-effort lock-down — owner read/write only.
    try:
        os.chmod(_AUTH_FILE, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def needs_setup() -> bool:
    """True when no passcode has been set yet (first boot)."""
    return _load_config() is None


def setup_passcode(passcode: str) -> None:
    """Initial passcode set. Refuses if one already exists."""
    if not passcode or len(passcode) < 4:
        raise ValueError("passcode must be at least 4 characters")
    if _load_config() is not None:
        raise ValueError("passcode already set; use change_passcode to update")
    salt = secrets.token_bytes(16)
    hashed = _hash_passcode(passcode, salt)
    _save_config(AuthConfig(
        passcode_hash=hashed.hex(),
        passcode_salt=salt.hex(),
        server_secret=secrets.token_hex(32),
    ))


def change_passcode(old: str, new: str) -> None:
    cfg = _load_config()
    if cfg is None:
        raise ValueError("no passcode set; call setup_passcode first")
    if not _verify_passcode(old, cfg):
        raise PermissionError("current passcode is wrong")
    if not new or len(new) < 4:
        raise ValueError("new passcode must be at least 4 characters")
    salt = secrets.token_bytes(16)
    hashed = _hash_passcode(new, salt)
    _save_config(AuthConfig(
        passcode_hash=hashed.hex(),
        passcode_salt=salt.hex(),
        # Rotate server secret so existing tokens are invalidated.
        server_secret=secrets.token_hex(32),
    ))


def _verify_passcode(passcode: str, cfg: AuthConfig) -> bool:
    salt = bytes.fromhex(cfg.passcode_salt)
    actual = _hash_passcode(passcode, salt)
    expected = bytes.fromhex(cfg.passcode_hash)
    return hmac.compare_digest(actual, expected)


def issue_token(passcode: str, device_label: str = "unknown") -> str:
    """Verify the passcode and return a signed token. Stores its hash so the
    token can be revoked later from the UI."""
    cfg = _load_config()
    if cfg is None:
        raise ValueError("no passcode set; call setup_passcode first")
    if not _verify_passcode(passcode, cfg):
        raise PermissionError("wrong passcode")
    serializer = URLSafeSerializer(cfg.server_secret, salt=_TOKEN_SALT)
    raw = secrets.token_urlsafe(32)
    signed = serializer.dumps({"r": raw, "d": device_label[:64]})
    db.store_token(_hash_token(signed), device_label[:64])
    return signed


def verify_token(token: str) -> bool:
    """Verify a bearer token. Two gates: signature valid (binds to server_secret)
    AND token hash is still in the DB (revocation)."""
    cfg = _load_config()
    if cfg is None:
        return False
    serializer = URLSafeSerializer(cfg.server_secret, salt=_TOKEN_SALT)
    try:
        serializer.loads(token)
    except BadSignature:
        return False
    return db.is_token_valid(_hash_token(token))


def revoke_token(token: str) -> None:
    db.revoke_token(_hash_token(token))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
