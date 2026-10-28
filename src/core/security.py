import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core.config import settings

bearer_scheme = HTTPBearer(auto_error=False)

MIN_JWT_SECRET_LENGTH = 32
# Service tokens (scripts/mint_service_token.py) use subjects with this prefix,
# so a service and a user can never share a subject or its sessions.
SERVICE_SUBJECT_PREFIX = "svc:"
# Placeholders that have shipped in this repo; never accept them as real secrets.
KNOWN_PLACEHOLDER_SECRETS = {
    "change-me-to-a-long-random-string",
    "dev-secret-change-me-0123456789abcdef",
}


def jwt_secret() -> str:
    """Return the signing secret, refusing missing, short or placeholder values."""
    secret = settings.JWT_SECRET
    if len(secret) < MIN_JWT_SECRET_LENGTH or secret in KNOWN_PLACEHOLDER_SECRETS:
        raise RuntimeError(
            f"JWT_SECRET must be a random string of at least {MIN_JWT_SECRET_LENGTH} "
            "characters (not the .env.example placeholder). Generate one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return secret


def create_access_token(
    subject: str, expires_minutes: int | None = None, token_id: str | None = None
) -> str:
    now = datetime.now(timezone.utc)
    minutes = expires_minutes or settings.JWT_EXPIRE_MINUTES
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    payload["jti"] = token_id or secrets.token_hex(8)
    return jwt.encode(payload, jwt_secret(), algorithm=settings.JWT_ALGORITHM)


def revoked_token_ids() -> set[str]:
    return {t.strip() for t in settings.REVOKED_TOKEN_IDS.split(",") if t.strip()}


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    try:
        payload = jwt.decode(
            credentials.credentials,
            jwt_secret(),
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.PyJWTError:
        raise unauthorized
    subject = payload.get("sub")
    if not subject:
        raise unauthorized
    if payload.get("jti") and payload["jti"] in revoked_token_ids():
        raise unauthorized
    return subject
