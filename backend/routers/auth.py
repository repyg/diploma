"""Authentication routes and coordinator bearer-token dependency."""

import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from ..schemas import LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


def _secret() -> str:
    value = os.getenv("JWT_SECRET_KEY", "")
    if len(value) < 32:
        raise RuntimeError("JWT_SECRET_KEY must be configured with at least 32 characters")
    return value


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, _secret(), algorithms=["HS256"])
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise JWTError("Missing subject")
        return subject
    except (JWTError, RuntimeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from None


@router.post("/login", response_model=TokenResponse)
def login(credentials: LoginRequest) -> TokenResponse:
    username = os.getenv("COORDINATOR_USERNAME", "")
    password = os.getenv("COORDINATOR_PASSWORD", "")
    if not username or not password:
        raise HTTPException(status_code=503, detail="Coordinator credentials are not configured")
    if not (secrets.compare_digest(credentials.username, username) and secrets.compare_digest(credentials.password, password)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    try:
        key = _secret()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="JWT signing key is not configured") from None
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": username, "iat": now, "exp": now + timedelta(hours=8)}, key, algorithm="HS256")
    return TokenResponse(access_token=token)
