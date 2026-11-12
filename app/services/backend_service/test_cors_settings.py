import os
import sys
import importlib.util
import pytest
import httpx
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


ROOT_APP_DIR = Path(__file__).resolve().parents[2]
SERVICE_DIR = Path(__file__).resolve().parent

for path in (str(SERVICE_DIR), str(ROOT_APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import app as root_app

# Service modules import `broker` from the service's top-level app.py when run
# in Docker. Give that import a harmless test value while loading this file by
# its unique module name, without replacing the repository's `app` package.
root_app.broker = object()

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("JWT_SECRET", "test-secret-for-auth-tests-only-32-bytes!!")

app_spec = importlib.util.spec_from_file_location(
    "backend_service_app",
    SERVICE_DIR / "app.py",
)
backend_app = importlib.util.module_from_spec(app_spec)
sys.modules[app_spec.name] = backend_app
app_spec.loader.exec_module(backend_app)
create_app = backend_app.create_app
from settings import get_settings


def test_settings_parse_configured_origins(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS",
        "https://app.example.com, http://localhost:5174/",
    )
    monkeypatch.setenv("CORS_ALLOW_ORIGIN_REGEX", "")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.cors_allow_origins == [
        "https://app.example.com",
        "http://localhost:5174",
    ]
    assert settings.cors_allow_origin_regex is None

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_preflight_allows_deployed_frontend_origin(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ALLOW_ORIGIN_REGEX", raising=False)
    get_settings.cache_clear()

    app = create_app(get_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/signup",
            headers={
                "Origin": "https://perpetual-trading-system.pushpitkamboj.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "https://perpetual-trading-system.pushpitkamboj.com"
    )
    assert response.headers["access-control-allow-credentials"] == "true"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_signup_signin_duplicate_and_password_validation(monkeypatch):
    from packages.db import Base
    import packages.db as db_module

    monkeypatch.setenv("JWT_SECRET", "test-secret-for-auth-tests-only-32-bytes!!")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", session_factory)

    try:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            signup = await client.post(
                "/signup",
                json={"username": "  e2e-user  ", "password": "secret123"},
            )
            assert signup.status_code == 201, signup.text
            assert signup.json()["username"] == "e2e-user"

            signin = await client.post(
                "/signin",
                json={"username": "e2e-user", "password": "secret123"},
            )
            assert signin.status_code == 200, signin.text
            assert signin.json()["user_id"] == signup.json()["user_id"]

            duplicate = await client.post(
                "/signup",
                json={"username": "e2e-user", "password": "secret123"},
            )
            assert duplicate.status_code == 409

            invalid_password = await client.post(
                "/signup",
                json={"username": "unicode-user", "password": "é" * 37},
            )
            assert invalid_password.status_code == 422

            invalid_login = await client.post(
                "/signin",
                json={"username": "e2e-user", "password": "wrong-pass"},
            )
            assert invalid_login.status_code == 401
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_private_websocket_snapshot_restores_balance_and_positions(monkeypatch):
    from packages.db import Base
    from packages.models import AccountBalance, PositionProjection

    websocket_module = sys.modules["routers.ws"]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(websocket_module, "SessionLocal", session_factory)

    class TestWebSocket:
        def __init__(self):
            self.events = []

        async def send_json(self, event):
            self.events.append(event)

    websocket = TestWebSocket()
    try:
        async with session_factory() as session:
            session.add(
                AccountBalance(
                    user_id="snapshot-user",
                    available=125,
                    locked=40,
                    wallet_balance=2000,
                )
            )
            session.add(
                PositionProjection(
                    user_id="snapshot-user",
                    market="BTC-PERP",
                    qty=0.5,
                    entry_price=100,
                    margin=25,
                    liquidation_price=50,
                    realized_pnl=3,
                )
            )
            await session.commit()

        await websocket_module._send_account_snapshot(websocket, "snapshot-user")

        assert websocket.events[0]["event_type"] == "balance_update"
        assert websocket.events[0]["payload"]["balance"] == 125
        assert websocket.events[0]["payload"]["locked"] == 40
        assert websocket.events[0]["payload"]["wallet_balance"] == 2000
        assert websocket.events[1]["event_type"] == "create_position"
        assert websocket.events[1]["payload"]["position"]["qty"] == 0.5
        assert websocket.events[1]["payload"]["position"]["entry"] == 100
        assert websocket.events[1]["payload"]["position"]["side"] == "long"
        assert websocket.events[1]["payload"]["position"]["leverage"] == 2
        assert websocket.events[1]["payload"]["position"]["realized_pnl"] == 3
    finally:
        await engine.dispose()

    client = TestClient(create_app(get_settings()))
    response = client.options(
        "/offramp",
        headers={
            "Origin": "http://localhost:5174",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5174"
    assert response.headers["access-control-allow-credentials"] == "true"

    get_settings.cache_clear()
