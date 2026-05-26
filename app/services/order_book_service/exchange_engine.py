from pydantic import BaseModel, Field
import heapq
import time

from packages.common_utils import MARKET, ORDER_TYPE, SIDE, STATUS

from app.worker.celery_app import celery

class Collateral(BaseModel):
    available: float
    locked: float

class Position(BaseModel):
    market: MARKET
    qty: float
    entry_price: float
    margin: float
    liquidation_price: float
    unrealized_pnl: float
    realized_pnl: float | None = None

# order → orderbook → match → fill → position update → live tracking → liquidation/close
class Fill(BaseModel):
    market: str
    price: float
    qty: float
    side: SIDE
    leverage: int # leverage of taker

class OrderRecord(BaseModel):
    user_id: str
    order_id: str
    market: MARKET
    side: SIDE
    qty: float
    margin: float
    leverage: float
    order_type: ORDER_TYPE
    price: float
    status: STATUS
    fills: list[Fill] = Field(default_factory=list)

class User(BaseModel):
    user_id: str
    username: str = Field(min_length=3, max_length=50)
    password: str
    collateral: Collateral

order_books: dict[str, dict[str, any]] = {} #market, minheap and maxheap storing price, time, order_id
users: dict[str, User] = {}
orders: dict[str, OrderRecord] = {} #order by id stored
position_by_user: dict[str, dict[MARKET, Position]] = {}
latest_index_prices: dict[str, float] = {}


def ensure_user_initialized(user_id: str, username: str | None = None):
    if user_id not in users:
        users[user_id] = User(
            user_id=user_id,
            username=username or f"user-{user_id[:8]}",
            password="",
            collateral=Collateral(available=0, locked=0),
        )
    if user_id not in position_by_user:
        position_by_user[user_id] = {}
    for market in MARKET.__args__:
        if market not in order_books:
            order_books[market] = {
                "min_heap": [],
                "max_heap": [],
            }

def check_margin_possible(order_id: str): #checks if the margin <= available
    current_order = orders[order_id]
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
    if current_order.side == "long":
        if not order_books[current_order.market]["min_heap"]:
            return False
        if current_order.price >= order_books[current_order.market]["min_heap"][0][0]:
            return True
    if current_order.side == "short":
        if order_books[current_order.market]["max_heap"]:
            if -order_books[current_order.market]["max_heap"][0][0] >= current_order.price:
                return True
    return False


def execute_current_order(order_id):
    current_order = orders[order_id]

    trade_price = None
    trade_qty = None

    best_order = None
    best_buy_order_id = None
    best_leverage = None

    opposite_type = "long" if current_order.side == "short" else "short"

    if current_order.side == "long":
        best_sell_price, order_dt, best_sell_order_id = order_books[current_order.market]["min_heap"][0]
        trade_price = best_sell_price
        best_order = orders[best_sell_order_id]

        trade_qty = min(current_order.qty, best_order.qty)

        current_order.qty -= trade_qty
        best_order.qty -= trade_qty

        best_order_user_id = best_order.user_id
        best_leverage = best_order.leverage
        update_order_status(order_id, best_sell_order_id)
    else:
        best_buy_price_neg, order_dt, best_buy_order_id = order_books[current_order.market]["max_heap"][0]
        trade_price = -best_buy_price_neg
        best_order = orders[best_buy_order_id]

        trade_qty = min(current_order.qty, best_order.qty)

        current_order.qty -= trade_qty
        best_order.qty -= trade_qty

        best_order_user_id = best_order.user_id
        best_leverage = best_order.leverage

        update_order_status(order_id, best_buy_order_id)

    fill_current = Fill(
        market = current_order.market,
        price = trade_price,
        qty = trade_qty,
        leverage = current_order.leverage,
        side = current_order.side
    )
    fill_best = Fill(
        market = current_order.market,
        price = trade_price,
        qty = trade_qty,
        leverage = best_leverage,
        side = opposite_type
    )
    current_order.fills.append(fill_current)
    best_order.fills.append(fill_best)
    return best_order_user_id, fill_current, fill_best


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
        if order_on_book.side == "long":
            heapq.heappop(order_books[order_on_book.market]["max_heap"])
        elif order_on_book.side == "short":
            heapq.heappop(order_books[order_on_book.market]["min_heap"])


def calculate_liquidation_price(price, margin, qty, side): #this is more of bankcrupcy price, there should be some relaxation gap, but for simplicity we kept bankcrupcy as liq. price
    if side == "long":
        return price - (margin/qty)
    elif side == "short":
        return price + (margin/qty)

def _get_signed_qty_change(fill: Fill): #in order to have 1 position per user per market, handling short/longs 
    if fill.side == "long":
        return fill.qty
    return -fill.qty

    
def update_entry_price_qty_margin(pos: Position, signed_fill_qty, fill_leverage, fill_price, user_id, market: MARKET):
    if pos.qty * signed_fill_qty > 0: # SAME DIRECTION
        pos.entry_price = (pos.entry_price*pos.qty + fill_price*signed_fill_qty) / (abs(pos.qty) + abs(signed_fill_qty))
        pos.margin += ((fill_price * abs(signed_fill_qty))/fill_leverage) #effective margin after combining the new fill
        pos.qty += signed_fill_qty #since position 
    elif pos.qty * signed_fill_qty < 0:
        pos_effective_price = pos.entry_price*abs(pos.qty)
        fill_effective_price = fill_price*abs(signed_fill_qty)
        if pos_effective_price == fill_effective_price: #NULLIFY
            user = users[user_id]
            user.collateral.locked -= pos.margin
            user.collateral.available += pos.margin #returned back the user money
            position_by_user[user_id][market] = None
            return
        elif pos_effective_price > fill_effective_price: # REDUCTION
            pos.entry_price = (pos.entry_price*pos.qty - fill_price*signed_fill_qty) / abs( abs(pos.qty) - abs(signed_fill_qty) )
            pos.qty = (pos.qty/abs(pos.qty)) * abs( abs(pos.qty) - abs(signed_fill_qty) ) #the sign should remain the same as earlier while the qty has to be reduced
        elif pos_effective_price < fill_effective_price:
            pos.realized_pnl = pos.unrealized_pnl # FLIP
            pos.entry_price = fill_price
            pos.qty = signed_fill_qty
            pos.margin = (fill_price * abs(signed_fill_qty))/fill_leverage
    

def calculate_position(user_id, market, fill: Fill):
    pos = position_by_user[user_id].get(market)
    if pos is None: #the position has not been created yet, its being created
        margin = (fill.price*fill.qty)/fill.leverage
        liquidation = calculate_liquidation_price(fill.price, margin, fill.qty, fill.side)
        pos = Position(
            entry_price=fill.price,
            market=market,
            qty=fill.qty,
            margin=margin,
            liquidation_price=liquidation,
            unrealized_pnl=0,
            realized_pnl=None
        )
        position_by_user[user_id][market] = pos
        return #the first position is created and stored
    signed_fill_qty = _get_signed_qty_change(fill) #pos for long, neg for short
    update_entry_price_qty_margin(pos, signed_fill_qty, fill.leverage, fill.price, user_id, market)
    pos.liquidation_price = calculate_liquidation_price(
        pos.entry_price, #newly effective price
        pos.margin,
        abs(pos.qty), #newly effective qty
        "long" if pos.qty > 0 else "short"
    )
    position_by_user[user_id][market] = pos


def place_order_on_orderbook(order_id):
   current_order = orders[order_id]
   current_order_price = current_order.price
   current_order_time = time.time()
   if current_order.side == "long":
        heapq.heappush(order_books[current_order.market]["max_heap"], (-current_order_price, current_order_time, order_id))
   elif current_order.side == "short":
       heapq.heappush(order_books[current_order.market]["min_heap"], (current_order_price, current_order_time, order_id))

#CREATE ENGINE OF POST ORDER
# all engine entrypoints run inside the Celery worker so state is mutated
# by exactly one process at a time. args/results cross the wire as JSON,
# so Pydantic models are dict-ified at the boundary.
@celery.task(name="create_order_engine")
def create_order_engine(order_dict: dict):
    current_order = OrderRecord(**order_dict)
    ensure_user_initialized(current_order.user_id)
    orders[current_order.order_id] = current_order # place the order before validation helpers read it
    if not check_margin_possible(current_order.order_id):
        orders.pop(current_order.order_id, None)
        return {
            "message": "sorry, you do not have sufficient balance in the account"
        }
    update_collateral(current_order.user_id, current_order.margin)

    while current_order_possible(current_order.order_id):
       best_order_book_order_id, fill_current, fill_best = execute_current_order(current_order.order_id)
       calculate_position(current_order.user_id, current_order.market, fill_current) # position of existing order
       calculate_position(best_order_book_order_id, current_order.market, fill_best) # position of other side of orderbook

    current_order_after_execution = orders[current_order.order_id]
    if current_order_after_execution.status != "filled":
       place_order_on_orderbook(current_order_after_execution.order_id)
    return {
        "order_id": current_order_after_execution.order_id,
        "status": current_order_after_execution.status,
        "remaining_qty": current_order_after_execution.qty,
        "fills": [fill.model_dump() for fill in current_order_after_execution.fills],
    }

    
#after this the positions aka real market starts to work         

def calculate_pnl(entry_price, index_price, qty):
    return (index_price - entry_price)*qty

def liquify_position(market, pos: Position, side: SIDE):
    order_qty = abs(pos.qty) #now treat it as on orderbook with market order
    while order_qty > 0:
        best_market_order = None
        best_market_order_side = side #NOTE: dont get confused by side, the liquify order was long - comes to orderbook at short and matches with the best order in long and then this long from orderbook to create fills and then position
        if side == "long": # if the liquify position was long then it will come to sell/short orderbook and get the best match from buy/long orderbook
            best_market_order_id = order_books[market]["max_heap"][0][2]
        elif side == "short":
            best_market_order_id = order_books[market]["min_heap"][0][2]
        best_market_order = orders[best_market_order_id]
        trade_qty = min(order_qty, best_market_order.qty)

        order_qty -= trade_qty
        best_market_order.qty -= trade_qty
        if best_market_order.qty > 0:
            best_market_order.status = "partially_filled"
        elif best_market_order.qty == 0:
            best_market_order.status = "filled"
            if best_market_order.side == "long":
                heapq.heappop(order_books[market]["max_heap"])
            elif best_market_order.side == "short":
                heapq.heappop(order_books[market]["min_heap"])
        fill = Fill(
            market=market,
            price=best_market_order.price,
            qty=trade_qty,
            side=best_market_order_side,
            leverage=best_market_order.leverage,
        )
        calculate_position(best_market_order.user_id, market, fill) #make the opposite order to the position which swapped with the liquidation at market price
    return best_market_order.price #this is what becomes the loss final of liquidation position also called the last price and the market order best price for close position
    

def update_account_after_liquidation(user_id, loss):
    user_collateral = users[user_id]
    user_collateral.collateral.locked = loss


@celery.task(name="on_price_update")
def on_price_update(market_index_prices: dict[str, float]):
    latest_index_prices.update(market_index_prices)
    #update the pnl for all the positions for all the users for all the market
    #it important to take snapshot and then update else it will mutate and make data dirty
    snapshot = [
        (user_id, mkt, pos)
        for user_id, mkts in position_by_user.items()
        for mkt, pos in mkts.items()
    ]

    for user_id, market, pos in snapshot:
        if position_by_user[user_id].get(market) is None: #if user's position is not there 
            continue
        index_price = market_index_prices.get(market)
        if index_price is None:
            continue
        side = "long" if pos.qty > 0 else "short"
        pos.unrealized_pnl = calculate_pnl(pos.entry_price, index_price, pos.qty)
        if side == "long" and index_price <= pos.liquidation_price: #to keep logic simpler, we are using index price as mark price
            last_trade_price = liquify_position(market, pos, side)
            update_account_after_close_position(user_id, last_trade_price, pos)
        elif side == "short" and index_price >= pos.liquidation_price:
            last_trade_price = liquify_position(market, pos, side)
            update_account_after_close_position(user_id, last_trade_price, pos) #conceptually its same logic for liquidation and close position


def update_account_after_close_position(user_id, exit_price, pos: Position):
    user = users[user_id]
    collateral = user.collateral
    # realized pnl from actual execution
    if pos.qty > 0:
        pos.realized_pnl = (exit_price - pos.entry_price) * abs(pos.qty)
    else:
        pos.realized_pnl = (pos.entry_price - exit_price) * abs(pos.qty)
    # release locked margin + apply pnl
    collateral.available += collateral.locked + pos.realized_pnl 
    collateral.locked = 0
    # reset position
    position_by_user[user_id][pos.market] = None


@celery.task(name="close_position")
def close_position(user_id, market):
    pos = position_by_user.get(user_id, {}).get(market)
    if pos is None:
        return {
            "message": "no open position"
        }
    pos.realized_pnl = pos.unrealized_pnl
    side = "long" if pos.qty > 0 else "short"
    close_position_at_market_order_price = liquify_position(market, pos, side) #conceptually both work similarly, the only difference is one is forced triggered and other is user triggered
    update_account_after_close_position(user_id, close_position_at_market_order_price, pos)
    return {
        "market": market,
        "exit_price": close_position_at_market_order_price,
        "realized_pnl": pos.realized_pnl,
        "message": "position closed",
    }



@celery.task(name="offramp")
def offramp(user_id, amount):
    ensure_user_initialized(user_id)
    user = users[user_id]
    if user.collateral.available < amount:
        return {
            "message": "not enough balance"
        }
    user.collateral.available -= amount
    return {
        "balance": user.collateral.available
    }

@celery.task(name="onramp")
def onramp(user_id, amount: float):
    ensure_user_initialized(user_id)
    user = users[user_id]
    user.collateral.available += amount
    return {
        "balance": user.collateral.available
    }

@celery.task(name="position_market")
def position_market(user_id, market: MARKET):
    ensure_user_initialized(user_id)
    user_position = position_by_user.get(user_id, {}).get(market)
    if user_position is None:
        return {
            "message": "no open position"
        }
    return user_position.model_dump()


    
