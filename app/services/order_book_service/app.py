from faststream.redis import RedisBroker
from faststream import FastStream

broker = RedisBroker("redis://localhost:6379")
app = FastStream(broker)

from create_order import create_order_engine
from on_price_update import on_price_update
from close_position import close_position
from balance_updates import balance_update
from state_recovery import restore_exchange_state

from typing import Annotated, Union
from pydantic import Field
from packages.common_utils import (
    OrderCreate, 
    BinancePrice,
    BalanceUpdate,
    ClosePosition,
    STREAM_PERPS
)


@app.on_startup
async def restore_state():
    await restore_exchange_state()

PerpsEvent = Annotated[
    Union[
        OrderCreate,
        BinancePrice,
        BalanceUpdate,
        ClosePosition
    ],
    Field(discriminator="event_type"),
]

@broker.subscriber(stream=STREAM_PERPS)
async def handle_perps_event(event: PerpsEvent):
    match event.event_type:
        case "order_create":
            await create_order_engine(event.payload)

        case "binance_data":
            await on_price_update(event.payload)

        case "close_position":
            # event is ClosePosition
            await close_position(event.payload)

        case "balance_update":
            # event is BalanceUpdate
            await balance_update(event.payload)
            
