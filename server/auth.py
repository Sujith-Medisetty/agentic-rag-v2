"""
Multi-user email + password auth with a root user materialised from .env.

Flow:
  1. First boot — server has no users yet → /api/auth/status returns
     {needs_setup: true, has_root: bool}. If OJAS_ROOT_EMAIL / PASSWORD are
     set in .env, the very first /api/auth/login with those credentials
     materialises the root user in the DB and returns a token. Otherwise the
     client posts to /api/auth/signup to create a first regular user.

  2. /api/auth/login posts {email, password}; we verify the password against
     the user's stored scrypt hash and return a signed token bound to the
     server secret. The token's hash is stored in auth_tokens alongside the
     user_id so it can be revoked AND the request handler can look up which
     user is calling.

  3. Each protected request sends `Authorization: Bearer <token>`. We verify
     the signature, look up the token's user_id, fetch the user (with role),
     and pass that to the handler.

Server secret + scrypt hashing same as before — only the user model changes.
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

# scrypt cost — interactive (~100ms on a modern Mac), plenty for a password.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


@dataclass
class ServerSecret:
    secret: str

    def to_json(self) -> str:
        return json.dumps({"server_secret": self.secret}, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "ServerSecret":
        d = json.loads(raw)
        return cls(secret=d["server_secret"])


# ---------------------------------------------------------------------------
# Server secret — used for itsdangerous signing. Created on first need.
# ---------------------------------------------------------------------------

def _load_or_create_secret() -> ServerSecret:
    if _AUTH_FILE.exists():
        try:
            return ServerSecret.from_json(_AUTH_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    s = ServerSecret(secret=secrets.token_hex(32))
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_FILE.write_text(s.to_json(), encoding="utf-8")
    try:
        os.chmod(_AUTH_FILE, 0o600)
    except OSError:
        pass
    return s


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


def _verify_password(password: str, stored_hash_hex: str, stored_salt_hex: str) -> bool:
    salt = bytes.fromhex(stored_salt_hex)
    actual = _hash_password(password, salt)
    expected = bytes.fromhex(stored_hash_hex)
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# Root user from .env
# ---------------------------------------------------------------------------

def _root_credentials() -> tuple[str, str] | None:
    """Return (email, password) from OJAS_ROOT_EMAIL / OJAS_ROOT_PASSWORD
    env vars, or None if either is missing. Password may be plaintext or a
    pre-hashed value via OJAS_ROOT_PASSWORD_HASH (not implemented yet —
    plaintext only for now)."""
    email = os.getenv("OJAS_ROOT_EMAIL", "").strip().lower()
    password = os.getenv("OJAS_ROOT_PASSWORD", "")
    if not email or not password:
        return None
    return email, password


def _ensure_root_user() -> dict | None:
    """If OJAS_ROOT_* are set AND no row exists for that email yet, create
    the root user. Returns the user dict, or None if no root creds are
    configured."""
    creds = _root_credentials()
    if creds is None:
        return None
    email, password = creds
    existing = db.get_user_by_email(email)
    if existing is not None:
        return existing
    salt = secrets.token_bytes(16)
    hashed = _hash_password(password, salt)
    return db.create_user(
        email=email,
        password_hash=hashed.hex(),
        password_salt=salt.hex(),
        role="root",
    )


def signup_allowed() -> bool:
    """Whether non-root users can create accounts. Defaults to True; set
    OJAS_ALLOW_SIGNUP=false in .env to lock it down."""
    raw = os.getenv("OJAS_ALLOW_SIGNUP", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def needs_setup() -> bool:
    """True when no users exist yet (first boot). The auth_status endpoint
    surfaces this so the UI can route to a signup screen / show the root
    credentials hint."""
    # If root creds are configured but root user hasn't been created yet, we
    # still consider the system "set up" — the login flow will materialise
    # root on first login attempt with the configured email/password.
    if _root_credentials() is not None:
        return False
    # No root configured; need at least one regular user.
    return len(db.list_users()) == 0


def has_root_configured() -> bool:
    """True when OJAS_ROOT_EMAIL + PASSWORD are present in env."""
    return _root_credentials() is not None


def signup(email: str, password: str) -> dict:
    """Create a regular user. Refuses if signup is disabled OR email taken."""
    if not signup_allowed():
        # Allow the FIRST user even when signup is disabled — there has to be
        # a way to bootstrap. Once any user exists, the gate kicks in.
        if len(db.list_users()) > 0:
            raise PermissionError("signup is disabled on this server")
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("email is required")
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    salt = secrets.token_bytes(16)
    hashed = _hash_password(password, salt)
    return db.create_user(
        email=email,
        password_hash=hashed.hex(),
        password_salt=salt.hex(),
        role="user",
    )


def reset_password(user_id: str, new_password: str) -> None:
    """Admin-only: overwrite the user's password hash + salt. Used to
    reset a forgotten password. New password is validated with the same
    rules as signup (>= 6 chars)."""
    if not new_password or len(new_password) < 6:
        raise ValueError("password must be at least 6 characters")
    salt = secrets.token_bytes(16)
    hashed = _hash_password(new_password, salt)
    if not db.update_user_password(
        user_id=user_id,
        password_hash=hashed.hex(),
        password_salt=salt.hex(),
    ):
        raise LookupError(f"user {user_id} not found")
    # Invalidate all existing sessions for this user — they have to log
    # in again with the new password.
    db.revoke_all_user_tokens(user_id)


def delete_user(user_id: str) -> None:
    """Admin-only: hard-delete a user row + all their auth tokens. Foreign
    keys (auth_tokens, projects, sessions) CASCADE per the schema;
    deployed_apps.owner_user_id SET NULLs (the admin endpoint fully
    tears down the apps BEFORE calling this, so nothing is orphaned).
    Caller must enforce the 'never delete the last root' rule before
    calling this."""
    # Drop auth tokens first — DB schema has ON DELETE CASCADE on
    # auth_tokens.user_id, so the DELETE on users will do it, but being
    # explicit makes the intent obvious in code.
    db.revoke_all_user_tokens(user_id)
    if not db.delete_user(user_id):
        raise LookupError(f"user {user_id} not found")


def login(email: str, password: str) -> tuple[dict, str]:
    """Verify credentials and return (user, signed_token). Raises
    PermissionError on bad credentials."""
    email = (email or "").strip().lower()
    if not email or not password:
        raise PermissionError("email + password required")

    # Special case: if env-configured root creds match, ensure the row
    # exists and proceed as that user. This is how the very first root
    # login bootstraps the user record.
    root_creds = _root_credentials()
    if root_creds is not None:
        root_email, root_password = root_creds
        if email == root_email and hmac.compare_digest(password, root_password):
            user = _ensure_root_user()
            if user is None:
                raise PermissionError("root user creation failed")
            return user, _issue_token(user["id"], device_label="root")

    # Regular DB lookup.
    user = db.get_user_by_email(email)
    if user is None:
        raise PermissionError("wrong email or password")
    if not _verify_password(password, user["password_hash"], user["password_salt"]):
        raise PermissionError("wrong email or password")
    return user, _issue_token(user["id"], device_label=email)


def _issue_token(user_id: str, device_label: str = "unknown") -> str:
    cfg = _load_or_create_secret()
    serializer = URLSafeSerializer(cfg.secret, salt=_TOKEN_SALT)
    raw = secrets.token_urlsafe(32)
    signed = serializer.dumps({"r": raw, "u": user_id})
    db.store_token(_hash_token(signed), device_label[:64], user_id=user_id)
    return signed


def user_from_token(token: str) -> dict | None:
    """Return the user dict (with role) the token belongs to, or None if
    the token is invalid / revoked. This is the new canonical lookup used
    by HTTP handlers that need to know who's calling."""
    return _user_from_token(token)


def _user_from_token(token: str) -> dict | None:
    if not token:
        return None
    cfg = _load_or_create_secret()
    serializer = URLSafeSerializer(cfg.secret, salt=_TOKEN_SALT)
    try:
        serializer.loads(token)
    except BadSignature:
        return None
    token_hash = _hash_token(token)
    if not db.is_token_valid(token_hash):
        return None
    user_id = db.get_token_user_id(token_hash)
    if not user_id:
        return None
    return db.get_user(user_id)


def revoke_token(token: str) -> None:
    db.revoke_token(_hash_token(token))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
