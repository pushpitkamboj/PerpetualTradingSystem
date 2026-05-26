from dotenv import load_dotenv
load_dotenv()
import os
from uuid import uuid4
import json
import asyncio
import logging
import websockets
from faststream import FastStream
from faststream.redis import RedisBroker
from packages.common_utils import STREAM_PERPS, BinancePrice, MARKET

BINANCE_WS = os.getenv("BINANCE_URL", "wss://fstream.binance.com/ws/!markPrice@arr")

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

async def listen_binance():
    while True:
        try:
            async with websockets.connect(BINANCE_WS) as ws:
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    market_index_prices: dict[MARKET, float] = {}
                    for asset in data:
                        market = SYMBOL_TO_MARKET.get(asset["s"])
                        if market:
                            market_index_prices[market] = float(asset["i"])
                    if not market_index_prices:
                        continue
                    message = BinancePrice(
                        event_type="binance_data",
                        payload=market_index_prices
                    )
                    await broker.publish(
                        message=message,
                        stream=STREAM_PERPS,
                        correlation_id=str(uuid4())
                    )
        except Exception as e:
            logger.exception("binance websocket listener failed; reconnecting", extra={"error": str(e)})
            await asyncio.sleep(3)
