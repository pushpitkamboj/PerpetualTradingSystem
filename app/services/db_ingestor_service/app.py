from faststream import FastStream
from faststream.redis import RedisBroker
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

@app.on_startup
async def startup():
    await init_db()


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
