from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.core.config import settings
from src.core.security import SERVICE_SUBJECT_PREFIX, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    username: str


@router.post("/token")
def issue_token(req: TokenRequest) -> dict:
    """Development helper: mint a JWT for testing. Disable with ENABLE_DEV_AUTH=false."""
    if not settings.ENABLE_DEV_AUTH:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if req.username.startswith(SERVICE_SUBJECT_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Usernames starting with {SERVICE_SUBJECT_PREFIX!r} are reserved for services",
        )
    token = create_access_token(req.username)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in_minutes": settings.JWT_EXPIRE_MINUTES,
    }
