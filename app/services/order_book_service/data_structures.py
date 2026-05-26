from pydantic import BaseModel, Field
from typing import Literal
from packages.common_utils import MARKET, SIDE, ORDER_TYPE, STATUS

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
# class Fill(BaseModel):
#     market: str
#     price: float
#     qty: float
#     side: SIDE
#     leverage: int # leverage of taker

class Fill(BaseModel):
    fill_id: str
    market: MARKET
    maker_order_id: str
    taker_order_id: str
    maker_user_id: str #already on table
    taker_user_id: str #just came on table
    price: float
    qty: float
    timestamp_ms: float

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

class User(BaseModel):
    user_id: str
    collateral: Collateral

class SyntheticOrder(BaseModel):
    user_id: str
    market: MARKET
    qty: float
    source: Literal["liquidation", "close_position"] # to differentiate between the source of synthetic order, as it can be created from both liquidation and close position, and we might want to have different handling for both in future
    

order_books: dict[str, dict[SIDE, any]] = {} #market, minheap and maxheap storing price, time, order_id
users: dict[str, User] = {}
orders: dict[str, OrderRecord] = {} #order by id stored
synthetic_orders: dict[str, SyntheticOrder] = {}
position_by_user: dict[str, dict[MARKET, Position]] = {}
fills: dict[str, Fill] = {} # fill by id stored

order_books["BTC-PERP"] = {
    "long": [], #maxheap based on price, timee.g. (-price, timestamp, order_id)
    "short": [] #minheap based on price, time e.g. (price, timestamp, order_id)
}
order_books["ETH-PERP"] = {
    "long": [],
    "short": []
}

