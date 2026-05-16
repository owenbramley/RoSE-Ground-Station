"""
Lightweight token-based authentication for the ground station.

Password is read from the GS_PASSWORD environment variable.
If unset, a random password is generated at startup and printed to stdout.

Flow:
  1. Client POSTs password to /api/auth  → receives a session token
  2. Token is sent as Authorization: Bearer <token> header on all API calls,
     or as ?token=<token> query param for img/websocket URLs
  3. Server validates token in require_auth dependency
"""
import logging
import os
import secrets

logger = logging.getLogger("auth")

# In-memory token store.  A simple set is sufficient — tokens are random
# and never expire during a server session.
_active_tokens: set[str] = set()


def get_password() -> str:
    """Return the configured password, generating one if GS_PASSWORD is unset."""
    return os.environ.get("GS_PASSWORD", "")


def _ensure_password() -> str:
    """Called once at startup to guarantee a password exists."""
    pw = get_password()
    if not pw:
        pw = secrets.token_urlsafe(12)
        os.environ["GS_PASSWORD"] = pw
        print(
            f"\n{'='*52}\n"
            f"  GS_PASSWORD not set — generated password:\n"
            f"  >>> {pw} <<<\n"
            f"  Set GS_PASSWORD env var to use a fixed password.\n"
            f"{'='*52}\n",
            flush=True,
        )
    return pw


# Ensure a password exists the moment this module is imported
_ensure_password()


def verify_password(password: str) -> bool:
    """Constant-time password comparison."""
    return secrets.compare_digest(password, get_password())


def create_token() -> str:
    """Create, store, and return a new session token."""
    token = secrets.token_urlsafe(32)
    _active_tokens.add(token)
    logger.info("New session token issued")
    return token


def validate_token(token: str | None) -> bool:
    """Return True if the token is valid."""
    if not token:
        return False
    return token in _active_tokens


def revoke_token(token: str):
    """Invalidate a specific token."""
    _active_tokens.discard(token)


def token_count() -> int:
    return len(_active_tokens)
