from update_account import update_account_after_close_position
from create_order import (
    _build_position_create_payload,
    calculate_position_from_fill,
    publish_orderbook_update,
    publish_position_create,
)
import heapq
import time
from uuid import uuid4

from data_structures import (
    Position,
    Fill,
    SyntheticOrder,           
    position_by_user,
    order_books,
    orders,
    synthetic_orders,
    fills,
)
from packages.common_utils import (
    BinancePrice,
    SIDE, 
    MARKET, 
    STREAM_PERPS_UPDATES,     
    LiquifyPositionPayload,
    LiquifyPositionData,
    UnrealizedPnlData,
    UnrealizedPnlPayload
)
from app import broker


def calculate_pnl(entry_price, index_price, qty):
    return (index_price - entry_price)*qty

async def on_price_update(market_index_prices: dict[str, float]):
    await broker.publish(
        message=BinancePrice(event_type="binance_data", payload=market_index_prices),
        stream=STREAM_PERPS_UPDATES,
        correlation_id=str(uuid4())
    )
    #update the pnl for all the positions for all the users for all the market
    #it important to take snapshot and then update else it will mutate and make data dirty
    snapshot = [
        (user_id, mkt, pos)
        for user_id, mkts in position_by_user.items()
        for mkt, pos in mkts.items()
    ]
    realized_pnl = None
    collateral_locked = None
    collateral_available = None
    last_unrealized_pnl = None

    for user_id, market, pos in snapshot:
        LIQUIFY = False
        if position_by_user.get(user_id, {}).get(market) is None: #if user's position is not there 
            continue
        index_price = market_index_prices.get(market)
        if index_price is None:
            continue
        side = "long" if pos.qty > 0 else "short"
        pos.unrealized_pnl = calculate_pnl(pos.entry_price, index_price, pos.qty)
        last_unrealized_pnl = pos.unrealized_pnl
        payload = UnrealizedPnlPayload(
            pnl=pos.unrealized_pnl,
            user_id=user_id,
            market=market
        )
        message = UnrealizedPnlData(
            event_type="unrealized_pnl", 
            payload=payload
        )
        await broker.publish(
            message=message,
            stream=STREAM_PERPS_UPDATES,
            correlation_id=str(uuid4())
        )
        if side == "long" and index_price <= pos.liquidation_price: #to keep logic simpler, we are using index price as mark price
            LIQUIFY = True
            last_trade_price = await _close_or_liquidify_position(market, pos, side, user_id, source="liquidation")
            if last_trade_price is None:
                continue
            realized_pnl, collateral_available, collateral_locked  = update_account_after_close_position(user_id, last_trade_price, pos)
        elif side == "short" and index_price >= pos.liquidation_price:
            LIQUIFY = True
            last_trade_price = await _close_or_liquidify_position(market, pos, side, user_id, source="liquidation")
            if last_trade_price is None:
                continue
            realized_pnl, collateral_available, collateral_locked = update_account_after_close_position(user_id, last_trade_price, pos) #conceptually its same logic for liquidation and close position
        if LIQUIFY:
            await publish_orderbook_update(market)
            payload = LiquifyPositionPayload(
                user_id=user_id,
                market=market,
                exit_price=last_trade_price,
                realized_pnl=realized_pnl,
                available=collateral_available,
                locked=collateral_locked,
            )
            message = LiquifyPositionData(
                event_type="liquify_position",
                payload=payload
            )
            await broker.publish(
                message=message,
                stream=STREAM_PERPS_UPDATES,
                correlation_id=str(uuid4())
            )
            return message
    return last_unrealized_pnl

async def _close_or_liquidify_position(market: MARKET, pos: Position, side: SIDE, user_id: str, source):
    # Check depth before consuming anything. A close or liquidation must not
    # partially mutate the book and then release collateral for the full size.
    available_qty = sum(
        orders[order_id].qty
        for _, _, order_id in order_books[market][side]
        if order_id in orders and orders[order_id].status != "filled"
    )
    order_qty = abs(pos.qty)
    if available_qty < order_qty:
        return None

    synthetic_order_id = str(uuid4())
    synthetic_orders[synthetic_order_id] = SyntheticOrder(
        user_id=user_id,
        market=market,
        qty=pos.qty,
        source=source
    )
    remaining_qty = order_qty
    executed_notional = 0.0
    matched_order_ids: set[str] = set()
    matched_fill_ids: list[str] = []
    while remaining_qty > 0:
        best_market_order = None #BEST means best available from that order book
        best_market_order_side = side #NOTE: dont get confused by side, the liquify order was long - comes to orderbook at short and matches with the best order in long and then this long from orderbook to create fills and then position
        best_market_order_id = order_books[market][best_market_order_side][0][2]
        best_market_order = orders[best_market_order_id]
        trade_qty = min(remaining_qty, best_market_order.qty)

        remaining_qty -= trade_qty
        executed_notional += best_market_order.price * trade_qty
        best_market_order.qty -= trade_qty
        if best_market_order.qty > 0:
            best_market_order.status = "partially_filled"
        elif best_market_order.qty == 0:
            best_market_order.status = "filled"
            heapq.heappop(order_books[market][side])

        fill_id = str(uuid4())
        fill = Fill(
            fill_id = fill_id,
            market=market,
            price=best_market_order.price,
            maker_order_id=best_market_order.order_id,
            taker_order_id=synthetic_order_id,
            maker_user_id=best_market_order.user_id,
            taker_user_id=user_id,
            qty=trade_qty,
            timestamp_ms=time.time()
        )
        fills[fill_id] = fill
        matched_fill_ids.append(fill_id)
        matched_order_ids.add(best_market_order_id)
        maker_pos = position_by_user.get(best_market_order.user_id, {}).get(market)
        maker_leverage = orders[best_market_order_id].leverage
        calculate_position_from_fill(maker_pos, fill, best_market_order_side, maker_leverage, best_market_order.user_id) #make the opposite order to the position which swapped with the liquidation at market price
    for matched_order_id in matched_order_ids:
        matched_order = orders[matched_order_id]
        await publish_position_create(
            _build_position_create_payload(
                user_id=matched_order.user_id,
                order_id=matched_order_id,
                market=market,
                fill_ids=[
                    fill_id
                    for fill_id in matched_fill_ids
                    if fills[fill_id].maker_order_id == matched_order_id
                ],
            )
        )

    # Use the VWAP when a close consumes more than one price level.
    return executed_notional / order_qty
    
