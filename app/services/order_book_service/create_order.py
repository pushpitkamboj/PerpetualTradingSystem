import heapq
import math
import time

from uuid import uuid4
from packages.common_utils import MARKET, OrderCreatePayload, STREAM_PERPS_UPDATES
from data_structures import (
    order_books,
    orders,
    position_by_user,
    users, 
    Fill,
    fills,
    SIDE,
    Position,
)
from app import broker
from balance_updates import _get_or_create_user
from packages.common_utils import (
    OrderBookDUpdateData,
    OrderBookLevel,
    OrderBookUpdatePayload,
    PositionCreateData,
    PositionCreatePayload,
)

def check_margin_possible(current_order: OrderCreatePayload): #checks if the margin <= available
    current_user_id = current_order.user_id
    current_user = users[current_user_id]
    current_user_collateral_available = current_user.collateral.available

    margin = (current_order.qty * current_order.price) / current_order.leverage
    return current_user_collateral_available >= margin


def update_collateral(user_id, margin): # after confirming, we apply locks to margin price
    current_user = users[user_id]
    current_user.collateral.available -= margin
    current_user.collateral.locked += margin


def current_order_possible(order_id: str):
    current_order = orders[order_id]
    if current_order.status == "filled":
        return False
    opposite_side = "short" if current_order.side == "long" else "long"
    if not order_books[current_order.market][opposite_side]: #if there is no order in opposite side of order book, then also current order cannot be executed, it has to go to order book
        return False
    if current_order.side == "long":
        if current_order.price >= order_books[current_order.market]["short"][0][0]:
            return True
    elif current_order.side == "short":
        if -order_books[current_order.market]["long"][0][0] >= current_order.price:
            return True
    return False


def execute_current_order(order_id):
    current_order = orders[order_id]
    trade_price = None
    trade_qty = None
    best_order = None
    best_order_id = None
    opposite_side = "short" if current_order.side == "long" else "long"

    best_price, order_dt, best_order_id = order_books[current_order.market][opposite_side][0]
    trade_price = best_price if opposite_side == "short" else -best_price
    best_order = orders[best_order_id]
    if best_order.user_id == current_order.user_id:
        # Do not let one account open a position against itself. Cancel the
        # incoming order in the caller while preserving the resting order.
        return None
    trade_qty = min(current_order.qty, best_order.qty)

    # Orders reserve collateral at their limit price. Reconcile each incoming
    # fill against the maker price before changing either order or the book.
    # This releases price improvement and refuses fills that would need more
    # collateral than the account has available.
    reserved_margin = current_order.price * trade_qty / current_order.leverage
    actual_margin = trade_price * trade_qty / current_order.leverage
    margin_delta = actual_margin - reserved_margin
    collateral = users[current_order.user_id].collateral
    if margin_delta > collateral.available:
        return None
    collateral.available -= margin_delta
    collateral.locked += margin_delta

    current_order.qty -= trade_qty
    best_order.qty -= trade_qty
    update_order_status(order_id, best_order_id)

    fill_id = str(uuid4())
    fill_current = Fill(
        fill_id = fill_id,
        market=current_order.market,
        maker_order_id=best_order_id,
        taker_order_id=order_id,
        maker_user_id=best_order.user_id,
        taker_user_id=current_order.user_id,
        price = trade_price,
        qty = trade_qty,
        timestamp_ms=time.time()
    )
    fills[fill_id] = fill_current
    return fill_current


def update_order_status(current_order_id, order_on_book_id):
    current_order = orders[current_order_id]
    order_on_book = orders[order_on_book_id]

    if current_order.qty > 0:
        current_order.status = "partially_filled"
    elif current_order.qty == 0:
        current_order.status = "filled"
    
    if order_on_book.qty > 0:
        order_on_book.status = "partially_filled"
    elif order_on_book.qty == 0:
        order_on_book.status = "filled"
        heapq.heappop(order_books[order_on_book.market][order_on_book.side])


def calculate_liquidation_price(price, margin, qty, side): #this is more of bankcrupcy price, there should be some relaxation gap, but for simplicity we kept bankcrupcy as liq. price
    if side == "long":
        return price - (margin/qty)
    elif side == "short":
        return price + (margin/qty)

def _get_signed_qty_change(side: SIDE, qty: float): #in order to have 1 position per user per market, handling short/longs
    if side == "long":
        return qty
    return -qty

def _calculate_realized_pnl(entry_price: float, exit_price: float, position_qty: float, closed_qty: float):
    if position_qty > 0:
        return (exit_price - entry_price) * closed_qty
    return (entry_price - exit_price) * closed_qty


def _cap_loss_to_margin(realized_pnl: float, allocated_margin: float) -> float:
    """Limit isolated-position losses to the collateral allocated to that slice."""
    return max(realized_pnl, -allocated_margin)


def _release_locked_collateral(user_id: str, margin_release: float, realized_pnl: float = 0):
    user = users[user_id]
    user.collateral.locked -= margin_release
    user.collateral.available += margin_release + realized_pnl


def _is_same_qty(lhs: float, rhs: float):
    return math.isclose(lhs, rhs, rel_tol=1e-9, abs_tol=1e-9)


def update_entry_price_qty_margin(pos: Position, signed_fill_qty, fill_leverage, fill_price, user_id, market: MARKET):
    fill_abs_qty = abs(signed_fill_qty)
    fill_margin = (fill_price * fill_abs_qty) / fill_leverage

    if pos.qty * signed_fill_qty > 0: # SAME DIRECTION
        new_qty = pos.qty + signed_fill_qty
        pos.entry_price = (
            (pos.entry_price * abs(pos.qty)) + (fill_price * fill_abs_qty)
        ) / abs(new_qty)
        pos.margin += fill_margin #effective margin after combining the new fill
        pos.qty = new_qty
        pos.realized_pnl = None
        pos.unrealized_pnl = 0
        return pos

    if pos.qty * signed_fill_qty >= 0:
        return pos

    old_qty = pos.qty
    old_abs_qty = abs(old_qty)
    closed_qty = min(old_abs_qty, fill_abs_qty)
    realized_pnl = _calculate_realized_pnl(
        entry_price=pos.entry_price,
        exit_price=fill_price,
        position_qty=old_qty,
        closed_qty=closed_qty,
    )
    released_position_margin = pos.margin * (closed_qty / old_abs_qty)
    realized_pnl = _cap_loss_to_margin(realized_pnl, released_position_margin)
    closing_fill_margin = (fill_price * closed_qty) / fill_leverage

    if _is_same_qty(fill_abs_qty, old_abs_qty): # CLOSE
        pos.realized_pnl = realized_pnl
        pos.unrealized_pnl = 0
        _release_locked_collateral(
            user_id=user_id,
            margin_release=pos.margin + closing_fill_margin,
            realized_pnl=realized_pnl,
        )
        position_by_user[user_id][market] = None
        return None

    if fill_abs_qty < old_abs_qty: # REDUCTION
        remaining_qty = old_qty + signed_fill_qty
        pos.qty = remaining_qty
        pos.margin -= released_position_margin
        pos.realized_pnl = realized_pnl
        pos.unrealized_pnl = 0
        _release_locked_collateral(
            user_id=user_id,
            margin_release=released_position_margin + fill_margin,
            realized_pnl=realized_pnl,
        )
        return pos

    residual_qty = fill_abs_qty - old_abs_qty # FLIP
    residual_signed_qty = math.copysign(residual_qty, signed_fill_qty)
    new_margin = (fill_price * residual_qty) / fill_leverage
    pos.realized_pnl = realized_pnl
    pos.unrealized_pnl = 0
    _release_locked_collateral(
        user_id=user_id,
        margin_release=pos.margin + closing_fill_margin,
        realized_pnl=realized_pnl,
    )
    pos.entry_price = fill_price
    pos.qty = residual_signed_qty
    pos.margin = new_margin
    return pos
    

def calculate_position_from_fill(pos: Position, fill: Fill, side: SIDE, leverage, user_id: str):
    if pos is None:
        margin = (fill.price*fill.qty) / leverage
        liquidation = calculate_liquidation_price(fill.price, margin, fill.qty, side)
        qty = fill.qty if side == "long" else -fill.qty
        pos = Position(
            entry_price=fill.price,
            market=fill.market,
            qty=qty,
            margin=margin,
            liquidation_price=liquidation,
            unrealized_pnl=0,
            realized_pnl=None
        )
        position_by_user[user_id][fill.market] = pos
    else:
        signed_fill_qty = _get_signed_qty_change(side, fill.qty)
        updated_pos = update_entry_price_qty_margin(pos, signed_fill_qty, leverage, fill.price, user_id, fill.market)
        if updated_pos is None:
            return
        updated_pos.liquidation_price = calculate_liquidation_price(
            updated_pos.entry_price, #newly effective price
            updated_pos.margin,
            abs(updated_pos.qty), #newly effective qty
            "long" if updated_pos.qty > 0 else "short"
        )


def place_order_on_orderbook(order_id):
   current_order = orders[order_id]
   current_order_price = current_order.price
   current_order_time = time.time()
   current_order_side = current_order.side
   if current_order_side == "long":
        heapq.heappush(order_books[current_order.market]["long"], (-current_order_price, current_order_time, order_id))
   elif current_order_side == "short":
       heapq.heappush(order_books[current_order.market]["short"], (current_order_price, current_order_time, order_id))


def _sort_orderbook_levels(levels: list[OrderBookLevel], side: SIDE):
    highest_price_first = True if side == "long" else False
    return sorted(
        levels,
        key=lambda level: level.price,
        reverse=highest_price_first
    )

def _get_orderbook_levels(market: MARKET, side: SIDE):
    levels = []
    for price, order_dt, order_id in order_books[market][side]:
        order = orders.get(order_id)
        if order is None or order.status == "filled":
            continue
        levels.append(
            OrderBookLevel(
                price=-price if side == "long" else price,
                qty=order.qty
            )
        )
    return _sort_orderbook_levels(levels, side)

async def publish_orderbook_update(market: MARKET):
    payload = OrderBookUpdatePayload(
        market=market,
        bids=_get_orderbook_levels(market, "long"),
        asks=_get_orderbook_levels(market, "short")
    )
    message = OrderBookDUpdateData(
        event_type="update_orderbook",
        payload=payload
    )
    await broker.publish(
        message=message,
        stream=STREAM_PERPS_UPDATES,
        correlation_id=str(uuid4())
    )


def _build_position_create_payload(user_id: str, order_id: str, market: MARKET, fill_ids: list[str]):
    position = position_by_user.get(user_id, {}).get(market)
    collateral = users[user_id].collateral
    position_data = position.model_dump() if position else None
    if position_data is not None:
        position_data.update(
            entry=position.entry_price,
            side="long" if position.qty > 0 else "short",
            leverage=(abs(position.qty) * position.entry_price / position.margin)
            if position.margin > 0
            else 1,
        )
    return PositionCreatePayload(
        user_id=user_id,
        order_id=order_id,
        market=market,
        status=orders[order_id].status,
        remaining_qty=orders[order_id].qty,
        fills=[fills[fill_id].model_dump() for fill_id in fill_ids],
        position=position_data,
        available=collateral.available,
        locked=collateral.locked,
        side=orders[order_id].side,
        order_type=orders[order_id].order_type,
        price=orders[order_id].price,
        leverage=orders[order_id].leverage,
    )


async def publish_position_create(payload: PositionCreatePayload):
    message = PositionCreateData(
        event_type="create_position",
        payload=payload
    )
    await broker.publish(
        message=message,
        stream=STREAM_PERPS_UPDATES,
        correlation_id=str(uuid4())
    )


#CREATE ENGINE OF POST ORDER
# all engine entrypoints run inside the Celery worker so state is mutated
# by exactly one process at a time. args/results cross the wire as JSON,
# so Pydantic models are dict-ified at the boundary.
async def create_order_engine(current_order: OrderCreatePayload):
    _get_or_create_user(current_order.user_id)
    position_by_user.setdefault(current_order.user_id, {})
    if not check_margin_possible(current_order):
        rejected_order = current_order.model_copy(update={"status": "rejected"})
        orders[rejected_order.order_id] = rejected_order
        rejection = _build_position_create_payload(
            user_id=rejected_order.user_id,
            order_id=rejected_order.order_id,
            market=rejected_order.market,
            fill_ids=[],
        )
        await publish_position_create(rejection)
        return rejection
    orders[current_order.order_id] = current_order # place the order before validation helpers read it
    update_collateral(current_order.user_id, current_order.margin)
    matched_fill_ids: list[str] = []
    matched_order_ids: set[str] = set()
    cancel_remainder = current_order.order_type == "market"

    while current_order_possible(current_order.order_id):
       fill_current = execute_current_order(current_order.order_id)
       if fill_current is None:
           cancel_remainder = True
           break
       matched_fill_ids.append(fill_current.fill_id)
       matched_order_ids.add(fill_current.maker_order_id)
       position_of_current_user = position_by_user.get(fill_current.taker_user_id, {}).get(current_order.market)
       position_of_swapped_user = position_by_user.get(fill_current.maker_user_id, {}).get(current_order.market)
       swapped_order_side = "long" if current_order.side == "short" else "short"
       swapped_order_id = fill_current.maker_order_id
       swapped_order_leverage = orders[swapped_order_id].leverage
       calculate_position_from_fill(position_of_current_user, fill_current, current_order.side, current_order.leverage, fill_current.taker_user_id) # position of incoming order
       calculate_position_from_fill(position_of_swapped_user, fill_current, swapped_order_side, swapped_order_leverage, fill_current.maker_user_id) # position of resting order

    current_order_after_execution = orders[current_order.order_id]
    if current_order_after_execution.status != "filled": # NO MATCH FOUND, STILL NOT FILLED, PUT IN ORDER BOOK
       if cancel_remainder:
           remaining_margin = (
               current_order_after_execution.qty
               * current_order_after_execution.price
               / current_order_after_execution.leverage
           )
           collateral = users[current_order_after_execution.user_id].collateral
           collateral.locked = max(0.0, collateral.locked - remaining_margin)
           collateral.available += remaining_margin
           current_order_after_execution.status = (
               "partially_filled_cancelled" if matched_fill_ids else "cancelled"
           )
       else:
           place_order_on_orderbook(current_order_after_execution.order_id)
    await publish_orderbook_update(current_order.market) #PUBLISH EVENT
    payload = _build_position_create_payload( #position for current user
        user_id=current_order.user_id,
        order_id=current_order_after_execution.order_id,
        market=current_order.market,
        fill_ids=matched_fill_ids,
    )
    await publish_position_create(payload)

    for matched_order_id in matched_order_ids:
        matched_order = orders[matched_order_id]
        matched_payload = _build_position_create_payload( #position for resting order
            user_id=matched_order.user_id,
            order_id=matched_order_id,
            market=matched_order.market,
            fill_ids=[
                fill_id
                for fill_id in matched_fill_ids
                if fills[fill_id].maker_order_id == matched_order_id
            ],
        )
        await publish_position_create(matched_payload)
    return payload # useful in tests/debugging
    
