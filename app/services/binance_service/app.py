from dotenv import load_dotenv
load_dotenv()
import os
from uuid import uuid4
import json
import asyncio
import logging
import math
import random
from urllib.error import HTTPError
from urllib.request import urlopen
import websockets
from faststream import FastStream
from faststream.redis import RedisBroker
from packages.common_utils import STREAM_PERPS, BinancePrice, MARKET

BINANCE_WS = os.getenv("BINANCE_URL", "wss://fstream.binance.com/ws/!markPrice@arr")
# Use Binance's public market-data-only spot endpoint for the REST fallback.
# Binance Futures can return HTTP 451 from regions where its futures API is not
# available. The app is a paper-trading simulator, so spot prices are a usable
# index proxy when the futures mark-price stream cannot be reached.
BINANCE_REST_URL = os.getenv(
    "BINANCE_REST_URL",
    "https://data-api.binance.vision/api/v3/ticker/price?symbols=%5B%22BTCUSDT%22,%22ETHUSDT%22%5D",
)
BINANCE_REST_POLL_INTERVAL_SECONDS = float(os.getenv("BINANCE_REST_POLL_INTERVAL_SECONDS", "1"))
BINANCE_HTTP_TIMEOUT_SECONDS = float(os.getenv("BINANCE_HTTP_TIMEOUT_SECONDS", "10"))
BINANCE_WS_MESSAGE_TIMEOUT_SECONDS = float(os.getenv("BINANCE_WS_MESSAGE_TIMEOUT_SECONDS", "8"))
SIMULATED_FALLBACK_ENABLED = os.getenv("ENABLE_SIMULATED_PRICE_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
SIMULATED_PRICE_VOLATILITY_BPS = float(os.getenv("SIMULATED_PRICE_VOLATILITY_BPS", "2"))
latest_market_prices: dict[MARKET, float] = {
    "BTC-PERP": float(os.getenv("SIMULATED_BTC_PRICE", "69000")),
    "ETH-PERP": float(os.getenv("SIMULATED_ETH_PRICE", "1940")),
}

SYMBOL_TO_MARKET: dict[str, MARKET] = {
    "BTCUSDT": "BTC-PERP",
    "ETHUSDT": "ETH-PERP",
}

broker = RedisBroker(os.getenv("REDIS_SERVER_URL", "redis://localhost:6379"))
app = FastStream(broker)
logger = logging.getLogger(__name__)

@app.after_startup #after the app has started and the broker is connected, start the binance listener
async def start_binance_listener():
    asyncio.create_task(listen_binance()) #NOTE: this can be a bad architecture design as the binance websocket listen is being blocked by the publisher async await, Its better to create 2 coroutines so binance WS can be truly async and store the data in a inmemory queue so then publisher can then push

def _extract_market_index_prices(payload) -> dict[MARKET, float]:
    assets = payload if isinstance(payload, list) else [payload]
    market_index_prices: dict[MARKET, float] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        symbol = asset.get("s") or asset.get("symbol")
        market = SYMBOL_TO_MARKET.get(symbol)
        if not market:
            continue
        raw_price = (
            asset.get("i")
            or asset.get("indexPrice")
            or asset.get("p")
            or asset.get("markPrice")
            or asset.get("price")
        )
        if raw_price is None:
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            market_index_prices[market] = price
    return market_index_prices

async def _publish_market_index_prices(
    market_index_prices: dict[MARKET, float],
    source: str = "binance_futures",
):
    if not market_index_prices:
        return
    latest_market_prices.update(market_index_prices)
    message = BinancePrice(
        event_type="binance_data",
        payload=market_index_prices,
        source=source,
    )
    await broker.publish(
        message=message,
        stream=STREAM_PERPS,
        correlation_id=str(uuid4())
    )


def _fetch_rest_snapshot() -> dict[MARKET, float]:
    with urlopen(BINANCE_REST_URL, timeout=BINANCE_HTTP_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    return _extract_market_index_prices(payload)


async def _listen_binance_websocket():
    async with websockets.connect(BINANCE_WS) as ws:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=BINANCE_WS_MESSAGE_TIMEOUT_SECONDS)
            data = json.loads(msg)
            await _publish_market_index_prices(
                _extract_market_index_prices(data),
                source="binance_futures",
            )


async def _poll_rest_forever():
    logger.warning("binance futures websocket unavailable; polling the public market-data endpoint")
    logged_first_snapshot = False
    logged_first_request = False
    logged_451 = False
    while True:
        try:
            if not logged_first_request:
                logger.info("requesting public Binance spot prices as the simulator index fallback")
                logged_first_request = True
            market_index_prices = await asyncio.to_thread(_fetch_rest_snapshot)
            if not market_index_prices:
                raise ValueError("ticker response contained no supported BTCUSDT or ETHUSDT price")
            await _publish_market_index_prices(market_index_prices, source="binance_spot")
            if market_index_prices and not logged_first_snapshot:
                logger.info(
                    "public Binance market-data fallback is publishing prices: %s",
                    market_index_prices,
                )
                logged_first_snapshot = True
        except HTTPError as exc:
            if exc.code == 451:
                if not logged_451:
                    body = exc.read(512).decode("utf-8", errors="replace").strip()
                    logger.error(
                        "Binance public market-data endpoint returned HTTP 451; "
                        "prices are unavailable from this server location: %s",
                        body or "no response body",
                    )
                    logged_451 = True
                await _publish_simulated_prices()
                await asyncio.sleep(BINANCE_REST_POLL_INTERVAL_SECONDS)
                continue
            logger.warning("binance public market-data request failed with HTTP %s", exc.code)
        except Exception as exc:
            logger.warning(
                "binance public market-data request failed (%s); retrying",
                type(exc).__name__,
            )
        await _publish_simulated_prices()
        await asyncio.sleep(BINANCE_REST_POLL_INTERVAL_SECONDS)


async def _publish_simulated_prices():
    if not SIMULATED_FALLBACK_ENABLED:
        return
    volatility = max(0.0, SIMULATED_PRICE_VOLATILITY_BPS) / 10_000
    simulated_prices = {
        market: max(0.01, price * (1 + random.gauss(0, volatility)))
        for market, price in latest_market_prices.items()
    }
    await _publish_market_index_prices(simulated_prices, source="local_simulation")


async def listen_binance():
    try:
        await _listen_binance_websocket()
    except asyncio.TimeoutError:
        logger.warning("binance websocket feed timed out; switching to REST polling fallback")
        await _poll_rest_forever()
    except Exception as exc:
        logger.exception("binance websocket listener failed; switching to REST polling", extra={"error": str(exc)})
        await _poll_rest_forever()
