import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


load_dotenv()

DEFAULT_LOCAL_CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1):\d+$"
DEFAULT_FRONTEND_ORIGINS = (
    "https://perpetual-trading-system.pushpitkamboj.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
)


def _split_csv_env(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip().rstrip("/") for part in value.split(",") if part.strip()]


@dataclass(frozen=True, slots=True)
class Settings:
    redis_server_url: str
    cors_allow_origins: list[str]
    cors_allow_origin_regex: str | None


@lru_cache
def get_settings() -> Settings:
    cors_allow_origins = _split_csv_env(os.getenv("CORS_ALLOW_ORIGINS"))
    if not cors_allow_origins:
        cors_allow_origins = list(DEFAULT_FRONTEND_ORIGINS)

    cors_allow_origin_regex = os.getenv(
        "CORS_ALLOW_ORIGIN_REGEX",
        DEFAULT_LOCAL_CORS_ORIGIN_REGEX,
    ).strip()

    return Settings(
        redis_server_url=os.getenv("REDIS_SERVER_URL", "redis://localhost:6379"),
        cors_allow_origins=cors_allow_origins,
        cors_allow_origin_regex=cors_allow_origin_regex or None,
    )
