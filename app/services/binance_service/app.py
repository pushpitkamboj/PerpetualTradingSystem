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
# Public Kraken spot ticker used when Binance is unavailable from the VM's
# region. This is an index-price proxy for paper markets, never an execution
# venue.
KRAKEN_REST_URL = os.getenv(
    "KRAKEN_REST_URL",
    "https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD",
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


def _extract_kraken_prices(payload) -> dict[MARKET, float]:
    """Read Kraken's public XBT/USD and ETH/USD last-trade ticker values."""
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Kraken ticker response contained an API error")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Kraken ticker response did not contain a result")

    prices: dict[MARKET, float] = {}
    for pair, ticker in result.items():
        normalized_pair = pair.upper()
        market = None
        if "XBT" in normalized_pair and "USD" in normalized_pair:
            market = "BTC-PERP"
        elif "ETH" in normalized_pair and "USD" in normalized_pair:
            market = "ETH-PERP"
        if market is None or not isinstance(ticker, dict):
            continue
        last_trade = ticker.get("c")
        raw_price = last_trade[0] if isinstance(last_trade, list) and last_trade else None
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            prices[market] = price
    return prices


def _fetch_kraken_snapshot() -> dict[MARKET, float]:
    with urlopen(KRAKEN_REST_URL, timeout=BINANCE_HTTP_TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    prices = _extract_kraken_prices(payload)
    if not prices:
        raise ValueError("Kraken ticker response contained no supported BTC/USD or ETH/USD price")
    return prices


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
    logger.warning("Binance futures websocket unavailable; trying public Binance and Kraken spot tickers")
    logged_binance_snapshot = False
    logged_kraken_snapshot = False
    logged_451 = False
    while True:
        try:
            market_index_prices = await asyncio.to_thread(_fetch_rest_snapshot)
            if not market_index_prices:
                raise ValueError("ticker response contained no supported BTCUSDT or ETHUSDT price")
            await _publish_market_index_prices(market_index_prices, source="binance_spot")
            if not logged_binance_snapshot:
                logger.info(
                    "public Binance spot ticker is publishing index prices: %s",
                    market_index_prices,
                )
                logged_binance_snapshot = True
        except HTTPError as exc:
            if exc.code == 451:
                if not logged_451:
                    logger.error(
                        "Binance public market-data endpoint returned HTTP 451; "
                        "trying Kraken's public spot ticker instead"
                    )
                    logged_451 = True
        except Exception as exc:
            logger.warning(
                "Binance public market-data request failed (%s); trying Kraken",
                type(exc).__name__,
            )

        try:
            market_index_prices = await asyncio.to_thread(_fetch_kraken_snapshot)
            await _publish_market_index_prices(market_index_prices, source="kraken_spot")
            if not logged_kraken_snapshot:
                logger.info("public Kraken spot ticker is publishing index prices: %s", market_index_prices)
                logged_kraken_snapshot = True
        except Exception as exc:
            logger.warning("Kraken public market-data request failed (%s); using configured simulator fallback", type(exc).__name__)
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
