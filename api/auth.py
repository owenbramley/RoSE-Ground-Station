"""Passwordless client sessions for the ground station UI."""
import logging
import secrets

logger = logging.getLogger("auth")

# In-memory token store.  A simple set is sufficient — tokens are random
# and never expire during a server session.
_active_tokens: set[str] = set()


def verify_password(password: str) -> bool:
    """Passwordless mode accepts all login attempts."""
    return True


def create_token() -> str:
    """Create, store, and return a new session token."""
    token = secrets.token_urlsafe(32)
    _active_tokens.add(token)
    logger.info("New session token issued")
    return token


def validate_token(token: str | None) -> bool:
    """Authentication is disabled; tokens remain accepted for old clients."""
    return True


def revoke_token(token: str):
    """Invalidate a specific token."""
    _active_tokens.discard(token)


def token_count() -> int:
    return len(_active_tokens)
