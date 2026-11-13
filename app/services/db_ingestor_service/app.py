import os

from faststream import FastStream
from faststream.redis import RedisBroker
from redis.asyncio import Redis
from packages.db import init_db, close_db
from typing import Union, Annotated
from pydantic import Field

from packages.common_utils import (
    STREAM_PERPS_UPDATES,
    BinancePrice,
    PositionCreateData,
    PositionCloseData,
    BalanceUpdateData,
    LiquifyPositionData,
    UnrealizedPnlData,
    OrderBookDUpdateData
)
from db_ingestion import (
    create_position, 
    close_position, 
    balance_update, 
    liquify_position
)
broker = RedisBroker("redis://localhost:6379")
app = FastStream(broker)
READY_KEY = f"perps:ready:db_ingestor:{os.getenv('HOSTNAME', 'unknown')}"

@app.on_startup
async def startup():
    client = Redis.from_url("redis://localhost:6379")
    await client.delete(READY_KEY)
    await client.aclose()
    await init_db()


@app.after_startup
async def mark_ready():
    client = Redis.from_url("redis://localhost:6379")
    await client.set(READY_KEY, "ready", ex=3600)
    await client.aclose()


@app.on_shutdown
async def shutdown():
    await close_db()


PerpsEvent = Annotated[
    Union[
        BinancePrice,
        PositionCreateData, # after position is created by the engine
        PositionCloseData,
        BalanceUpdateData,
        LiquifyPositionData,
        UnrealizedPnlData,
        OrderBookDUpdateData
    ],
    Field(discriminator="event_type"),
]


@broker.subscriber(stream=STREAM_PERPS_UPDATES)
async def handle_perps_updates(event: PerpsEvent):
    match event.event_type:
        case "binance_data":
            return

        case "create_position":
            await create_position(event.payload) 
        
        case "close_position":
            await close_position(event.payload)
        
        case "balance_update":
            await balance_update(event.payload)
        
        case "liquify_position":
            await liquify_position(event.payload)

        case "unrealized_pnl":
            return
        
        case "update_orderbook": #not to be processed by engine 
            return
