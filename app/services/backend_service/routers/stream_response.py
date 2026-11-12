from connection_manager import manager


def _message(event_type: str, payload):
    return {
        "event_type": event_type,
        "payload": payload.model_dump()
    }


async def create_position(payload):
    await manager.send_to_user(
        payload.user_id,
        _message("create_position", payload)
    )


async def binance_data(payload):
    for market, price in payload.items():
        await manager.send_to_market(
            market,
            {
                "event_type": "binance_data",
                "payload": {
                    "market": market,
                    "price": price,
                },
            }
        )


async def close_position(payload):
    await manager.send_to_user(
        payload.user_id,
        _message("close_position", payload)
    )


async def balance_update(payload):
    await manager.send_to_user(
        payload.user_id,
        _message("balance_update", payload)
    )


async def liquify_position(payload):
    await manager.send_to_user(
        payload.user_id,
        _message("liquify_position", payload)
    )


async def unrealized_pnl(payload):
    await manager.send_to_user(
        payload.user_id,
        _message("unrealized_pnl", payload)
    )


async def update_orderbook(payload):
    await manager.send_to_market(
        payload.market,
        _message("update_orderbook", payload)
    )
