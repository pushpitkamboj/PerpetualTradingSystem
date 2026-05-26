from update_account import update_account_after_close_position
from create_order import calculate_position_from_fill, publish_orderbook_update
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
)
from packages.common_utils import (
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

    for user_id, market, pos in snapshot:
        LIQUIFY = False
        if position_by_user.get(user_id, {}).get(market) is None: #if user's position is not there 
            continue
        index_price = market_index_prices.get(market)
        if index_price is None:
            continue
        side = "long" if pos.qty > 0 else "short"
        pos.unrealized_pnl = calculate_pnl(pos.entry_price, index_price, pos.qty)
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
            last_trade_price = _close_or_liquidify_position(market, pos, side, user_id, source="liquidation")
            realized_pnl, collateral_available, collateral_locked  = update_account_after_close_position(user_id, last_trade_price, pos)
        elif side == "short" and index_price >= pos.liquidation_price:
            LIQUIFY = True
            last_trade_price = _close_or_liquidify_position(market, pos, side, user_id, source="liquidation")
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
        return pos.unrealized_pnl

def _close_or_liquidify_position(market: MARKET, pos: Position, side: SIDE, user_id: str, source):
    synthetic_order_id = str(uuid4())
    synthetic_orders[synthetic_order_id] = SyntheticOrder(
        user_id=user_id,
        market=market,
        qty=pos.qty,
        source=source
    )
    order_qty = abs(pos.qty) #now treat it as on orderbook with market order
    while order_qty > 0:
        best_market_order = None #BEST means best available from that order book
        best_market_order_side = side #NOTE: dont get confused by side, the liquify order was long - comes to orderbook at short and matches with the best order in long and then this long from orderbook to create fills and then position
        best_market_order_id = order_books[market][best_market_order_side][0][2]
        best_market_order = orders[best_market_order_id]
        trade_qty = min(order_qty, best_market_order.qty)

        order_qty -= trade_qty
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
        maker_pos = position_by_user.get(best_market_order.user_id, None).get(market)
        maker_leverage = orders[best_market_order_id].leverage
        calculate_position_from_fill(maker_pos, fill, best_market_order_side, maker_leverage, best_market_order.user_id) #make the opposite order to the position which swapped with the liquidation at market price
    return best_market_order.price #this is what becomes the loss final of liquidation position also called the last price and the market order best price for close position
    
