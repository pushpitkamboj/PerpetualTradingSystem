import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import websockets


MARKETS = ("BTC-PERP", "ETH-PERP")
SIDES = ("long", "short")
LEVERAGES = (1, 2, 5, 10, 20)
BASE_PRICES = {
    "BTC-PERP": 63320.0,
    "ETH-PERP": 3480.0,
}

API_BASE_URL = os.getenv("BOT_API_BASE_URL", "http://fastapi:8000")
WS_BASE_URL = os.getenv("BOT_WS_BASE_URL", "ws://fastapi:8000")
BOT_COUNT = int(os.getenv("BOT_COUNT", "10"))
BOT_PASSWORD = os.getenv("BOT_PASSWORD", "secret123")
STARTING_BALANCE = float(os.getenv("BOT_STARTING_BALANCE", "100000"))
MIN_SLEEP_SECONDS = float(os.getenv("BOT_MIN_SLEEP_SECONDS", "1.5"))
MAX_SLEEP_SECONDS = float(os.getenv("BOT_MAX_SLEEP_SECONDS", "7.0"))
ORDER_PROBABILITY = float(os.getenv("BOT_ORDER_PROBABILITY", "0.82"))
CLOSE_PROBABILITY = float(os.getenv("BOT_CLOSE_PROBABILITY", "0.18"))

Market = Literal["BTC-PERP", "ETH-PERP"]
Side = Literal["long", "short"]

logger = logging.getLogger("liquidity_bot")


@dataclass
class MarketView:
    price: float
    bids: list[dict[str, float]] = field(default_factory=list)
    asks: list[dict[str, float]] = field(default_factory=list)


@dataclass
class BotUser:
    username: str
    token: str
    user_id: str
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    balance: float = 0


class LiquidityBot:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=API_BASE_URL, timeout=10)
        self.market_view = {
            market: MarketView(price=BASE_PRICES[market])
            for market in MARKETS
        }
        self.users: list[BotUser] = []

    async def run(self):
        await self.wait_for_api()
        self.users = await self.bootstrap_users()
        public_task = asyncio.create_task(self.public_market_listener())
        private_tasks = [
            asyncio.create_task(self.private_user_listener(user))
            for user in self.users
        ]
        trader_tasks = [
            asyncio.create_task(self.trade_loop(user, index))
            for index, user in enumerate(self.users)
        ]
        await asyncio.gather(public_task, *private_tasks, *trader_tasks)

    async def wait_for_api(self):
        for attempt in range(90):
            try:
                response = await self.client.get(f"{API_BASE_URL}/docs")
                if response.status_code < 500:
                    logger.info("fastapi is reachable")
                    return
            except Exception:
                pass
            await asyncio.sleep(min(1 + attempt * 0.1, 5))
        raise RuntimeError("fastapi did not become reachable")

    async def bootstrap_users(self):
        users = []
        for index in range(BOT_COUNT):
            username = f"perp_bot_{index + 1:02d}"
            session = await self.signup_or_signin(username)
            user = BotUser(
                username=session["username"],
                token=session["token"],
                user_id=session["user_id"],
            )
            await self.onramp(user, STARTING_BALANCE)
            users.append(user)
            logger.info("bootstrapped %s", username)
        return users

    async def signup_or_signin(self, username: str):
        payload = {"username": username, "password": BOT_PASSWORD}
        signup = await self.client.post("/signup", json=payload)
        if signup.status_code == 201:
            return signup.json()
        if signup.status_code != 409:
            signup.raise_for_status()

        signin = await self.client.post("/signin", json=payload)
        signin.raise_for_status()
        return signin.json()

    async def onramp(self, user: BotUser, amount: float):
        response = await self.client.post(
            "/onramp",
            json={"amount": amount},
            headers=self.auth_headers(user),
        )
        response.raise_for_status()

    async def public_market_listener(self):
        while True:
            try:
                async with websockets.connect(f"{WS_BASE_URL}/ws/market") as websocket:
                    for market in MARKETS:
                        await websocket.send(json.dumps({"type": "subscribe_market", "market": market}))
                    logger.info("public market websocket connected")

                    async for raw_message in websocket:
                        event = json.loads(raw_message)
                        event_type = event.get("event_type")
                        payload = event.get("payload", {})
                        if event_type == "update_orderbook":
                            market = payload["market"]
                            self.market_view[market].bids = payload.get("bids", [])
                            self.market_view[market].asks = payload.get("asks", [])
                        elif event_type == "binance_data":
                            market = payload["market"]
                            self.market_view[market].price = float(payload["price"])
            except Exception as exc:
                logger.warning("public websocket reconnecting: %s", exc)
                await asyncio.sleep(2)

    async def private_user_listener(self, user: BotUser):
        while True:
            try:
                async with websockets.connect(f"{WS_BASE_URL}/ws?token={user.token}") as websocket:
                    logger.info("private websocket connected for %s", user.username)
                    async for raw_message in websocket:
                        event = json.loads(raw_message)
                        self.apply_private_event(user, event)
            except Exception as exc:
                logger.warning("private websocket reconnecting for %s: %s", user.username, exc)
                await asyncio.sleep(2)

    def apply_private_event(self, user: BotUser, event: dict[str, Any]):
        event_type = event.get("event_type")
        payload = event.get("payload", {})
        if event_type == "balance_update":
            user.balance = float(payload.get("balance", user.balance))
        elif event_type == "create_position":
            position = payload.get("position")
            market = payload.get("market")
            if position and market:
                user.positions[market] = position
            elif market:
                user.positions.pop(market, None)
        elif event_type == "unrealized_pnl":
            market = payload.get("market")
            if market in user.positions:
                user.positions[market]["unrealized_pnl"] = payload.get("pnl", 0)
        elif event_type in {"close_position", "liquify_position"}:
            market = payload.get("market")
            if market:
                user.positions.pop(market, None)
            user.balance = float(payload.get("available", user.balance))

    async def trade_loop(self, user: BotUser, index: int):
        await asyncio.sleep(2 + index * 0.35)
        while True:
            await asyncio.sleep(random.uniform(MIN_SLEEP_SECONDS, MAX_SLEEP_SECONDS))
            try:
                if user.positions and random.random() < CLOSE_PROBABILITY:
                    market = random.choice(list(user.positions.keys()))
                    await self.close_position(user, market)
                    continue

                if random.random() <= ORDER_PROBABILITY:
                    await self.place_order(user)
            except httpx.HTTPStatusError as exc:
                logger.warning("%s http error: %s", user.username, exc.response.text)
            except Exception as exc:
                logger.warning("%s loop error: %s", user.username, exc)

    async def place_order(self, user: BotUser):
        market = random.choice(MARKETS)
        side = random.choice(SIDES)
        view = self.market_view[market]
        leverage = random.choice(LEVERAGES)
        qty = self.random_qty(market)
        price = self.best_working_price(view, side)

        response = await self.client.post(
            "/order",
            json={
                "market": market,
                "side": side,
                "order_type": "limit",
                "qty": qty,
                "price": price,
                "leverage": leverage,
            },
            headers=self.auth_headers(user),
        )
        response.raise_for_status()
        body = response.json()
        logger.info(
            "%s placed %s %s qty=%s price=%s lev=%sx order=%s",
            user.username,
            market,
            side,
            qty,
            price,
            leverage,
            body.get("order_id", "")[:8],
        )

    async def close_position(self, user: BotUser, market: str):
        response = await self.client.post(
            f"/close-position?market={market}",
            headers=self.auth_headers(user),
        )
        response.raise_for_status()
        logger.info("%s requested close for %s", user.username, market)

    def best_working_price(self, view: MarketView, side: Side):
        spread_bps = random.uniform(-0.0018, 0.0018)
        should_cross = random.random() < 0.62

        if side == "long" and should_cross and view.asks:
            price = float(view.asks[0]["price"]) * (1 + random.uniform(0, 0.0003))
        elif side == "short" and should_cross and view.bids:
            price = float(view.bids[0]["price"]) * (1 - random.uniform(0, 0.0003))
        else:
            price = view.price * (1 + spread_bps)

        decimals = 2 if view.price > 1000 else 3
        return round(price, decimals)

    def random_qty(self, market: str):
        if market == "BTC-PERP":
            return round(random.uniform(0.001, 0.035), 4)
        return round(random.uniform(0.02, 0.55), 3)

    def auth_headers(self, user: BotUser):
        return {"Authorization": f"Bearer {user.token}"}


async def main():
    logging.basicConfig(
        level=os.getenv("BOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    bot = LiquidityBot()
    try:
        await bot.run()
    finally:
        await bot.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
