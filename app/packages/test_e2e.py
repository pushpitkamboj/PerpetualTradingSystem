import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets


pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8000")
WS_URL = os.getenv("E2E_WS_URL", "ws://127.0.0.1:8000/ws")
PASSWORD = "secret123"
SUPPORTED_MARKET = "BTC-PERP"


@dataclass(frozen=True)
class AuthSession:
    username: str
    user_id: str
    token: str


def run_command(command: list[str], input_text: str | None = None, timeout: int = 90) -> str:
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def compose(*args: str) -> str:
    return run_command(["docker", "compose", *args])


def redis_cli(*args: str) -> str:
    return run_command(["docker", "exec", "python_cex_redis", "redis-cli", *args]).strip()


@pytest.fixture(autouse=True)
def fresh_runtime_state() -> None:
    if os.getenv("E2E_SKIP_RECREATE") != "1":
        compose("up", "-d", "--force-recreate", "fastapi", "orderbook", "db_ingestor")
        time.sleep(2)
    redis_cli("DEL", "perps", "perps_updates")


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as async_client:
        yield async_client


async def post(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> httpx.Response:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await client.post(path, json=payload, headers=headers)


async def signup_and_signin(client: httpx.AsyncClient, label: str) -> AuthSession:
    username = f"e2e_{label}_{int(time.time() * 1000)}"
    signup = await post(client, "/signup", {"username": username, "password": PASSWORD})
    assert signup.status_code == 201, signup.text

    signin = await post(client, "/signin", {"username": username, "password": PASSWORD})
    assert signin.status_code == 200, signin.text
    body = signin.json()
    return AuthSession(username=username, user_id=body["user_id"], token=body["token"])


async def recv_json(websocket, timeout: float = 7) -> dict[str, Any]:
    message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
    return json.loads(message)


async def collect_events(websocket, count: int, timeout: float = 7) -> list[dict[str, Any]]:
    return [await recv_json(websocket, timeout=timeout) for _ in range(count)]


def publish_synthetic_price(market: str, price: float) -> None:
    script = f"""
import asyncio
from uuid import uuid4
from faststream.redis import RedisBroker
from packages.common_utils import STREAM_PERPS, BinancePrice

async def main():
    broker = RedisBroker('redis://redis:6379')
    await broker.start()
    await broker.publish(
        message=BinancePrice(event_type='binance_data', payload={{{market!r}: {price!r}}}),
        stream=STREAM_PERPS,
        correlation_id=str(uuid4()),
    )
    await broker.stop()

asyncio.run(main())
"""
    run_command(
        ["docker", "exec", "-i", "python_perps-fastapi-1", "python", "-"],
        input_text=script,
        timeout=30,
    )


def publish_orderbook_update(market: str) -> None:
    script = f"""
import asyncio
from uuid import uuid4
from faststream.redis import RedisBroker
from packages.common_utils import STREAM_PERPS_UPDATES, OrderBookDUpdateData, OrderBookUpdatePayload, OrderBookLevel

async def main():
    broker = RedisBroker('redis://redis:6379')
    await broker.start()
    await broker.publish(
        message=OrderBookDUpdateData(
            event_type='update_orderbook',
            payload=OrderBookUpdatePayload(
                market={market!r},
                bids=[OrderBookLevel(price=123, qty=1)],
                asks=[],
            ),
        ),
        stream=STREAM_PERPS_UPDATES,
        correlation_id=str(uuid4()),
    )
    await broker.stop()

asyncio.run(main())
"""
    run_command(
        ["docker", "exec", "-i", "python_perps-fastapi-1", "python", "-"],
        input_text=script,
        timeout=30,
    )


def db_snapshot(user1_id: str, user2_id: str) -> dict[str, Any]:
    script = f"""
import json
import sqlite3

conn = sqlite3.connect('/app/test.db')
cur = conn.cursor()

def scalar(query, args=()):
    return cur.execute(query, args).fetchone()[0]

balance = cur.execute(
    'SELECT available, locked FROM account_balances WHERE user_id=?',
    ({user1_id!r},),
).fetchone()

print(json.dumps({{
    'user1_balance': list(balance) if balance else None,
    'user1_orders': scalar('SELECT COUNT(*) FROM order_projections WHERE user_id=?', ({user1_id!r},)),
    'user2_orders': scalar('SELECT COUNT(*) FROM order_projections WHERE user_id=?', ({user2_id!r},)),
    'user1_fills': scalar('SELECT COUNT(*) FROM fill_projections WHERE maker_user_id=? OR taker_user_id=?', ({user1_id!r}, {user1_id!r})),
    'user1_positions': scalar('SELECT COUNT(*) FROM position_projections WHERE user_id=?', ({user1_id!r},)),
}}))
"""
    output = run_command(
        ["docker", "exec", "-i", "python_perps-db_ingestor-1", "python", "-"],
        input_text=script,
        timeout=30,
    )
    return json.loads(output)


def db_user_snapshot(user_id: str) -> dict[str, Any]:
    script = f"""
import json
import sqlite3

conn = sqlite3.connect('/app/test.db')
cur = conn.cursor()

def scalar(query, args=()):
    return cur.execute(query, args).fetchone()[0]

balance = cur.execute(
    'SELECT available, locked FROM account_balances WHERE user_id=?',
    ({user_id!r},),
).fetchone()

position = cur.execute(
    'SELECT qty, entry_price, margin, liquidation_price, realized_pnl FROM position_projections WHERE user_id=?',
    ({user_id!r},),
).fetchone()

print(json.dumps({{
    'balance': list(balance) if balance else None,
    'orders': scalar('SELECT COUNT(*) FROM order_projections WHERE user_id=?', ({user_id!r},)),
    'fills': scalar('SELECT COUNT(*) FROM fill_projections WHERE maker_user_id=? OR taker_user_id=?', ({user_id!r}, {user_id!r})),
    'positions': scalar('SELECT COUNT(*) FROM position_projections WHERE user_id=?', ({user_id!r},)),
    'position': list(position) if position else None,
}}))
"""
    output = run_command(
        ["docker", "exec", "-i", "python_perps-db_ingestor-1", "python", "-"],
        input_text=script,
        timeout=30,
    )
    return json.loads(output)


async def create_open_position(
    client: httpx.AsyncClient,
    session: AuthSession,
    market: str = SUPPORTED_MARKET,
) -> None:
    onramp = await post(client, "/onramp", {"amount": 2000}, token=session.token)
    assert onramp.status_code == 202
    await asyncio.sleep(0.5)

    long_order = await post(
        client,
        "/order",
        {
            "market": market,
            "side": "long",
            "order_type": "limit",
            "qty": 3,
            "price": 200,
            "leverage": 2,
        },
        token=session.token,
    )
    assert long_order.status_code == 201
    await asyncio.sleep(0.5)

    short_order = await post(
        client,
        "/order",
        {
            "market": market,
            "side": "short",
            "order_type": "limit",
            "qty": 3,
            "price": 200,
            "leverage": 2,
        },
        token=session.token,
    )
    assert short_order.status_code == 201
    await asyncio.sleep(0.7)


@pytest.mark.asyncio
async def test_auth_and_rest_validation_edges(client: httpx.AsyncClient) -> None:
    session = await signup_and_signin(client, "auth")

    duplicate = await post(
        client,
        "/signup",
        {"username": session.username, "password": PASSWORD},
    )
    assert duplicate.status_code == 409

    bad_signin = await post(
        client,
        "/signin",
        {"username": session.username, "password": "wrong-password"},
    )
    assert bad_signin.status_code == 401

    unauthenticated_order = await post(
        client,
        "/order",
        {
            "market": SUPPORTED_MARKET,
            "side": "long",
            "order_type": "limit",
            "qty": 1,
            "price": 100,
            "leverage": 1,
        },
    )
    assert unauthenticated_order.status_code == 401

    invalid_leverage = await post(
        client,
        "/order",
        {
            "market": SUPPORTED_MARKET,
            "side": "long",
            "order_type": "limit",
            "qty": 1,
            "price": 100,
            "leverage": 99,
        },
        token=session.token,
    )
    assert invalid_leverage.status_code == 422


@pytest.mark.asyncio
async def test_websocket_auth_and_subscription_edges(client: httpx.AsyncClient) -> None:
    session = await signup_and_signin(client, "ws_edges")

    with pytest.raises(Exception):
        async with websockets.connect(f"{WS_URL}?token=bad-token"):
            pass

    async with websockets.connect(f"{WS_URL}?token={session.token}") as websocket:
        await websocket.send(json.dumps({"type": "ping"}))
        assert await recv_json(websocket) == {"event_type": "pong", "payload": {}}

        await websocket.send(json.dumps({"type": "subscribe_market", "market": "DOGE-PERP"}))
        unsupported = await recv_json(websocket)
        assert unsupported["event_type"] == "error"
        assert unsupported["payload"]["message"] == "unsupported market"

        await websocket.send(json.dumps({"type": "subscribe_market", "market": SUPPORTED_MARKET}))
        subscribed = await recv_json(websocket)
        assert subscribed == {
            "event_type": "market_subscribed",
            "payload": {"market": SUPPORTED_MARKET},
        }


@pytest.mark.asyncio
async def test_private_events_are_not_sent_to_other_users(client: httpx.AsyncClient) -> None:
    owner = await signup_and_signin(client, "private_owner")
    watcher = await signup_and_signin(client, "private_watcher")

    async with (
        websockets.connect(f"{WS_URL}?token={owner.token}") as owner_ws,
        websockets.connect(f"{WS_URL}?token={watcher.token}") as watcher_ws,
    ):
        owner_onramp = await post(client, "/onramp", {"amount": 1000}, token=owner.token)
        assert owner_onramp.status_code == 202

        owner_event = await recv_json(owner_ws)
        assert owner_event["event_type"] == "balance_update"
        assert owner_event["payload"]["user_id"] == owner.user_id

        with pytest.raises(asyncio.TimeoutError):
            await recv_json(watcher_ws, timeout=0.5)


@pytest.mark.asyncio
async def test_order_with_insufficient_margin_is_not_projected(client: httpx.AsyncClient) -> None:
    session = await signup_and_signin(client, "margin")

    onramp = await post(client, "/onramp", {"amount": 50}, token=session.token)
    assert onramp.status_code == 202
    await asyncio.sleep(0.7)

    order = await post(
        client,
        "/order",
        {
            "market": SUPPORTED_MARKET,
            "side": "long",
            "order_type": "limit",
            "qty": 3,
            "price": 200,
            "leverage": 2,
        },
        token=session.token,
    )
    assert order.status_code == 201
    await asyncio.sleep(0.7)

    snapshot = db_user_snapshot(session.user_id)
    assert snapshot["balance"] == [50.0, 0.0]
    assert snapshot["orders"] == 0
    assert snapshot["fills"] == 0
    assert snapshot["positions"] == 0
    assert int(redis_cli("XLEN", "perps_updates")) == 1


@pytest.mark.asyncio
async def test_offramp_success_and_insufficient_balance_projection(client: httpx.AsyncClient) -> None:
    session = await signup_and_signin(client, "offramp")

    async with websockets.connect(f"{WS_URL}?token={session.token}") as websocket:
        onramp = await post(client, "/onramp", {"amount": 1000}, token=session.token)
        assert onramp.status_code == 202
        onramp_event = await recv_json(websocket)
        assert onramp_event["payload"]["balance"] == 1000

        offramp = await post(client, "/offramp", {"amount": 300}, token=session.token)
        assert offramp.status_code == 202
        offramp_event = await recv_json(websocket)
        assert offramp_event["event_type"] == "balance_update"
        assert offramp_event["payload"]["balance"] == 700

        too_large = await post(client, "/offramp", {"amount": 999}, token=session.token)
        assert too_large.status_code == 202
        failed_event = await recv_json(websocket)
        assert failed_event["payload"]["message"] == "not enough balance to withdraw"
        assert failed_event["payload"]["balance"] == 700

    snapshot = db_user_snapshot(session.user_id)
    assert snapshot["balance"] == [700.0, 0.0]


@pytest.mark.asyncio
async def test_market_subscription_isolation_and_unsubscribe(client: httpx.AsyncClient) -> None:
    session = await signup_and_signin(client, "markets")

    async with websockets.connect(f"{WS_URL}?token={session.token}") as websocket:
        await websocket.send(json.dumps({"type": "subscribe_market", "market": SUPPORTED_MARKET}))
        assert (await recv_json(websocket))["event_type"] == "market_subscribed"

        publish_orderbook_update("ETH-PERP")
        with pytest.raises(asyncio.TimeoutError):
            await recv_json(websocket, timeout=0.5)

        publish_orderbook_update(SUPPORTED_MARKET)
        btc_event = await recv_json(websocket)
        assert btc_event["event_type"] == "update_orderbook"
        assert btc_event["payload"]["market"] == SUPPORTED_MARKET

        await websocket.send(json.dumps({"type": "unsubscribe_market", "market": SUPPORTED_MARKET}))
        assert (await recv_json(websocket))["event_type"] == "market_unsubscribed"

        publish_orderbook_update(SUPPORTED_MARKET)
        with pytest.raises(asyncio.TimeoutError):
            await recv_json(websocket, timeout=0.5)


@pytest.mark.asyncio
async def test_partial_fill_updates_orderbook_depth(client: httpx.AsyncClient) -> None:
    long_user = await signup_and_signin(client, "partial_long")
    short_user = await signup_and_signin(client, "partial_short")

    async with websockets.connect(f"{WS_URL}?token={long_user.token}") as websocket:
        await websocket.send(json.dumps({"type": "subscribe_market", "market": SUPPORTED_MARKET}))
        assert (await recv_json(websocket))["event_type"] == "market_subscribed"

        long_onramp = await post(client, "/onramp", {"amount": 5000}, token=long_user.token)
        assert long_onramp.status_code == 202
        assert (await recv_json(websocket))["event_type"] == "balance_update"

        resting = await post(
            client,
            "/order",
            {
                "market": SUPPORTED_MARKET,
                "side": "long",
                "order_type": "limit",
                "qty": 5,
                "price": 200,
                "leverage": 2,
            },
            token=long_user.token,
        )
        assert resting.status_code == 201
        resting_events = await collect_events(websocket, 2)
        assert "update_orderbook" in [event["event_type"] for event in resting_events]

        short_onramp = await post(client, "/onramp", {"amount": 5000}, token=short_user.token)
        assert short_onramp.status_code == 202
        await asyncio.sleep(0.5)

        partial = await post(
            client,
            "/order",
            {
                "market": SUPPORTED_MARKET,
                "side": "short",
                "order_type": "limit",
                "qty": 2,
                "price": 200,
                "leverage": 2,
            },
            token=short_user.token,
        )
        assert partial.status_code == 201

        depth_event = await recv_json(websocket)
        assert depth_event["event_type"] == "update_orderbook"
        assert depth_event["payload"]["bids"] == [{"price": 200.0, "qty": 3.0}]
        assert depth_event["payload"]["asks"] == []


@pytest.mark.asyncio
async def test_liquidation_updates_streams_and_removes_position_projection(client: httpx.AsyncClient) -> None:
    owner = await signup_and_signin(client, "liquidated")
    liquidity = await signup_and_signin(client, "liquidator_liquidity")

    await create_open_position(client, owner)

    async with websockets.connect(f"{WS_URL}?token={owner.token}") as websocket:
        await websocket.send(json.dumps({"type": "subscribe_market", "market": SUPPORTED_MARKET}))
        assert (await recv_json(websocket))["event_type"] == "market_subscribed"

        liquidity_onramp = await post(client, "/onramp", {"amount": 5000}, token=liquidity.token)
        assert liquidity_onramp.status_code == 202
        await asyncio.sleep(0.5)

        liquidity_order = await post(
            client,
            "/order",
            {
                "market": SUPPORTED_MARKET,
                "side": "long",
                "order_type": "limit",
                "qty": 3,
                "price": 90,
                "leverage": 2,
            },
            token=liquidity.token,
        )
        assert liquidity_order.status_code == 201
        assert (await recv_json(websocket))["event_type"] == "update_orderbook"

        publish_synthetic_price(SUPPORTED_MARKET, 100.0)
        liquidation_events = await collect_events(websocket, 3)

    event_types = [event["event_type"] for event in liquidation_events]
    assert "unrealized_pnl" in event_types
    assert "update_orderbook" in event_types
    assert "liquify_position" in event_types

    liquify_event = next(event for event in liquidation_events if event["event_type"] == "liquify_position")
    assert liquify_event["payload"]["user_id"] == owner.user_id
    assert liquify_event["payload"]["market"] == SUPPORTED_MARKET

    snapshot = db_user_snapshot(owner.user_id)
    assert snapshot["positions"] == 0
    assert snapshot["balance"] is not None


@pytest.mark.asyncio
async def test_full_flow_websocket_fanout_and_db_ingestion(client: httpx.AsyncClient) -> None:
    owner = await signup_and_signin(client, "owner")
    liquidity = await signup_and_signin(client, "liquidity")

    async with websockets.connect(f"{WS_URL}?token={owner.token}") as websocket:
        await websocket.send(json.dumps({"type": "subscribe_market", "market": SUPPORTED_MARKET}))
        assert (await recv_json(websocket))["event_type"] == "market_subscribed"

        onramp = await post(client, "/onramp", {"amount": 2000}, token=owner.token)
        assert onramp.status_code == 202
        onramp_events = await collect_events(websocket, 1)

        long_order = await post(
            client,
            "/order",
            {
                "market": SUPPORTED_MARKET,
                "side": "long",
                "order_type": "limit",
                "qty": 3,
                "price": 200,
                "leverage": 2,
            },
            token=owner.token,
        )
        assert long_order.status_code == 201
        resting_events = await collect_events(websocket, 2)

        short_order = await post(
            client,
            "/order",
            {
                "market": SUPPORTED_MARKET,
                "side": "short",
                "order_type": "limit",
                "qty": 3,
                "price": 200,
                "leverage": 2,
            },
            token=owner.token,
        )
        assert short_order.status_code == 201
        matching_events = await collect_events(websocket, 2)

        liquidity_onramp = await post(client, "/onramp", {"amount": 2000}, token=liquidity.token)
        assert liquidity_onramp.status_code == 202

        liquidity_order = await post(
            client,
            "/order",
            {
                "market": SUPPORTED_MARKET,
                "side": "long",
                "order_type": "limit",
                "qty": 3,
                "price": 250,
                "leverage": 2,
            },
            token=liquidity.token,
        )
        assert liquidity_order.status_code == 201
        liquidity_events = await collect_events(websocket, 1)

        publish_synthetic_price(SUPPORTED_MARKET, 250.0)
        pnl_events = await collect_events(websocket, 1)

        close = await post(client, f"/close-position?market={SUPPORTED_MARKET}", token=owner.token)
        assert close.status_code == 202
        close_events = await collect_events(websocket, 2)

    all_events = onramp_events + resting_events + matching_events + liquidity_events + pnl_events + close_events
    event_types = [event["event_type"] for event in all_events]

    assert "balance_update" in event_types
    assert "create_position" in event_types
    assert "update_orderbook" in event_types
    assert "unrealized_pnl" in event_types
    assert "close_position" in event_types

    snapshot = db_snapshot(owner.user_id, liquidity.user_id)
    assert snapshot["user1_balance"] is not None
    assert snapshot["user1_orders"] >= 2
    assert snapshot["user2_orders"] >= 1
    assert snapshot["user1_fills"] >= 1
    assert snapshot["user1_positions"] == 0


@pytest.mark.asyncio
async def test_stream_counts_and_clean_service_logs(client: httpx.AsyncClient) -> None:
    session = await signup_and_signin(client, "streams")

    onramp = await post(client, "/onramp", {"amount": 1000}, token=session.token)
    assert onramp.status_code == 202

    order = await post(
        client,
        "/order",
        {
            "market": SUPPORTED_MARKET,
            "side": "long",
            "order_type": "limit",
            "qty": 1,
            "price": 100,
            "leverage": 1,
        },
        token=session.token,
    )
    assert order.status_code == 201
    await asyncio.sleep(1)

    assert int(redis_cli("XLEN", "perps")) >= 2
    assert int(redis_cli("XLEN", "perps_updates")) >= 3

    for service in ("fastapi", "orderbook", "db_ingestor"):
        logs = compose("logs", "--tail=260", service)
        assert "Traceback" not in logs
        assert "ValidationError" not in logs
