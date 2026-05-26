from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.db import get_session
from packages.models import User
from security import create_token, hash_password, verify_password
from pydantic import BaseModel, Field

class AuthRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=6)

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
    await session.commit()
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
