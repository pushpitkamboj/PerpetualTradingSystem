from data_structures import (
    position_by_user,
    MARKET
)
from uuid import uuid4
from packages.common_utils import STREAM_PERPS_UPDATES, ClosePositionDetails, PositionCloseData, PositionClosePayload
from update_account import update_account_after_close_position
from on_price_update import _close_or_liquidify_position
from create_order import publish_orderbook_update
from app import broker

async def close_position(payload: ClosePositionDetails):
    user_id = payload.user_id
    market = payload.market
    pos = position_by_user.get(user_id, {}).get(market)
    if pos is None:
        return {
            "message": "no open position"
        }
    side = "long" if pos.qty > 0 else "short"
    close_position_at_market_order_price = _close_or_liquidify_position(market, pos, side, user_id, source="close_position") #conceptually both work similarly, the only difference is one is forced triggered and other is user triggered
    realized_pnl, available, locked = update_account_after_close_position(user_id, close_position_at_market_order_price, pos)
    await publish_orderbook_update(market)
    payload = PositionClosePayload(
        user_id=user_id,
        market=market,
        available = available,
        locked = locked,
        exit_price=close_position_at_market_order_price,
        realized_pnl=realized_pnl,
        message= "position closed"
    )
    message = PositionCloseData(
        event_type="close_position",
        payload=payload
    )
    await broker.publish(
        message=message,
        stream=STREAM_PERPS_UPDATES,
        correlation_id=str(uuid4())
    )
    return message
