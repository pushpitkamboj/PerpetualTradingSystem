from dotenv import load_dotenv
load_dotenv()
import os
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

def _get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return database_url


engine = create_async_engine(
    _get_database_url(), 
    echo=False, # SQLAlchemy engine, echo=False is dont print logs
    pool_pre_ping=True, # checks if a pooled connection is dead before using it
    pool_size=4, # base number of reusable DB connections kept ready
    max_overflow=30, # extra temporary connections allowed when traffic exceeds pool_size
    pool_timeout=10 # how long to wait for a connection if pool is exhausted
) 
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn: #open an async context manager
        await conn.run_sync(Base.metadata.create_all) # run a synchronous SQLAlchemy function through async connection, it creates all tables defined in that metadata

async def close_db() -> None:
    await engine.dispose() # gracefully close all connections in the pool

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session: # creates 1 session from the connection pool/factory
        yield session
