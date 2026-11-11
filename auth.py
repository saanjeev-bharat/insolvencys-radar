
"""Security utilities for Insolvency Radar customer accounts.

Handles Argon2 password hashing and JWT access tokens.
Never store passwords or signing secrets in plain text.
"""

import os
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash


# Password hashing using Argon2
password_hasher = PasswordHash.recommended()

# JWT configuration
JWT_ALGORITHM = "HS256"
TOKEN_LIFETIME_HOURS = 12


def _get_secret_key():
    """Read the signing key securely from the environment."""
    secret = os.environ.get("RADAR_SECRET_KEY")

    if not secret or len(secret) < 32:
        raise RuntimeError(
            "RADAR_SECRET_KEY is missing or too short. "
            "Configure a secure secret key in the environment."
        )

    return secret


def hash_password(password: str) -> str:
    """Create a secure Argon2 password hash."""
    if not isinstance(password, str) or len(password) < 10:
        raise ValueError("Password must contain at least 10 characters.")

    if len(password) > 128:
        raise ValueError("Password must not exceed 128 characters.")

    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its stored hash."""
    try:
        return password_hasher.verify(password, password_hash)
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int, email: str) -> str:
    """Create a signed JWT access token."""
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=TOKEN_LIFETIME_HOURS),
    }

    return jwt.encode(
        payload,
        _get_secret_key(),
        algorithm=JWT_ALGORITHM
    )


def decode_access_token(token: str) -> dict:
    """Verify a JWT and return its claims."""
    return jwt.decode(
        token,
        _get_secret_key(),
        algorithms=[JWT_ALGORITHM]
    )
