from dotenv import load_dotenv
load_dotenv()
import os
from fastapi import FastAPI
from contextlib import asynccontextmanager
from faststream.redis import RedisBroker
from packages.db import init_db, close_db
from packages.common_utils import (
    STREAM_PERPS_UPDATES,
    PositionCreateData,
    BinancePrice,
    PositionCloseData,
    BalanceUpdateData,
    LiquifyPositionData,
    OrderBookDUpdateData,
    UnrealizedPnlData
)
from pydantic import Field
from typing import Annotated, Union
broker = RedisBroker(os.getenv("REDIS_SERVER_URL", "redis://localhost:6379"))
from routers.stream_response import (
    create_position,
    binance_data,
    close_position,
    balance_update,
    liquify_position,
    update_orderbook,
    unrealized_pnl
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("fastapi started")
    await broker.start()
    await init_db()
    yield
    await broker.stop()
    await close_db()

    print("Shutdown code here")

app = FastAPI(lifespan=lifespan)

from routers.auth import router as auth_router
from routers.exchange import router as exchange_router
from routers.ws import router as ws_router

app.include_router(auth_router)
app.include_router(exchange_router)
app.include_router(ws_router)

PerpsUpdatesEvent = Annotated[
    Union[
        PositionCreateData,
        BinancePrice,
        PositionCloseData,
        BalanceUpdateData,
        LiquifyPositionData,
        OrderBookDUpdateData,
        UnrealizedPnlData

    ],
    Field(discriminator="event_type"),
]


@broker.subscriber(stream=STREAM_PERPS_UPDATES)
async def handle_perps_event(event: PerpsUpdatesEvent):
    match event.event_type:
        case "create_position":
            await create_position(event.payload)

        case "binance_data":
            await binance_data(event.payload)

        case "close_position":
            await close_position(event.payload)
        
        case "balance_update":
            await balance_update(event.payload)

        case "liquify_position":
            await liquify_position(event.payload)

        case "unrealized_pnl":
            await unrealized_pnl(event.payload)

        case "update_orderbook":
            await update_orderbook(event.payload)
