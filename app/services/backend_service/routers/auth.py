from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, field_validator

from packages.db import get_session
from packages.models import User
from security import create_token, hash_password, verify_password
class AuthRequest(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=6)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, username: str) -> str:
        username = username.strip()
        if not username:
            raise ValueError("username must not be blank")
        return username

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, password: str) -> str:
        # bcrypt supports passwords up to 72 bytes. Count encoded bytes so
        # multi-byte Unicode input cannot trigger an internal hashing error.
        if len(password.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 UTF-8 bytes")
        return password

class AuthResponse(BaseModel):
    token: str
    user_id: str
    username: str

router = APIRouter(tags=["auth"])

@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: AuthRequest,
    session: AsyncSession = Depends(get_session),
):
    existing_user = await session.scalar(select(User).where(User.username == payload.username))
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username already exists",
        )
    user = User(
        username=payload.username,
        password=hash_password(payload.password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username already exists",
        ) from exc
    # engine-side init happens lazily inside every Celery task via ensure_user_initialized()
    return AuthResponse(
        token=create_token(user.id),
        user_id=user.id,
        username=user.username,
    )


@router.post("/signin", response_model=AuthResponse)
async def signin(
    payload: AuthRequest,
    session: AsyncSession = Depends(get_session),
) -> AuthResponse:
    user = await session.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )

    return AuthResponse(
        token=create_token(user.id),
        user_id=user.id,
        username=user.username,
    )
