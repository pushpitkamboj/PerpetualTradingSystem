from typing import Literal
from pydantic import BaseModel, Field

MARKET = Literal["BTC-PERP", "ETH-PERP"]
SIDE = Literal["long", "short"]  # long = buy, short = sell
ORDER_TYPE = Literal["limit", "market"]
STATUS = Literal[
    "filled",
    "binance_data",
    "open",
    "partially_filled",
    "partially_filled_cancelled",
    "cancelled",
    "rejected",
]

MAX_LEVERAGE = 20
MIN_LEVERAGE = 1
STREAM_PERPS = "perps" #redis stream, (equivalent to topics in kafka)
STREAM_PERPS_UPDATES = "perps_updates" # to publish updates related to positions, pnl, balance after order execution, liquidation, close position etc.

class OrderCreatePayload(BaseModel):
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

class OrderCreate(BaseModel):
    event_type: Literal["order_create"]
    payload: OrderCreatePayload

class BinancePrice(BaseModel):
    event_type: Literal["binance_data"]
    payload: dict[MARKET, float]

class BalanceDetails(BaseModel):
    update_type: Literal["onramp", "offramp"]
    user_id: str
    amount: float

class BalanceUpdate(BaseModel):
    event_type: Literal["balance_update"]
    payload: BalanceDetails
    
class ClosePositionDetails(BaseModel):
    user_id: str
    market: MARKET

class ClosePosition(BaseModel):
    event_type: Literal["close_position"]
    payload: ClosePositionDetails
    
#STREAM_PERPS_UPDATES models
class Position(BaseModel):
    market: MARKET
    qty: float
    entry_price: float
    margin: float
    liquidation_price: float
    unrealized_pnl: float
    realized_pnl: float | None = None
    # Frontend-friendly aliases retained alongside the canonical API fields.
    entry: float | None = None
    side: SIDE | None = None
    leverage: float | None = None

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

class PositionCreatePayload(BaseModel):
    user_id: str
    order_id: str
    market: MARKET
    status: STATUS
    remaining_qty: float
    fills: list[Fill]
    position: Position | None
    available: float | None = None
    locked: float | None = None
    side: SIDE | None = None
    order_type: ORDER_TYPE | None = None
    price: float | None = None
    leverage: float | None = None
    
class PositionCreateData(BaseModel):
    event_type: Literal["create_position"]
    payload: PositionCreatePayload

class PositionClosePayload(BaseModel):
    user_id: str
    market: MARKET
    exit_price: float
    realized_pnl: float
    message: str
    available: float = Field(ge=0)
    locked: float = Field(ge=0)

class PositionCloseData(BaseModel):
    event_type: Literal["close_position"]
    payload: PositionClosePayload

class BalanceUpdatePayload(BaseModel):
    user_id: str
    message: str
    balance: float
    locked: float = 0
    wallet_balance: float | None = None
    update_type: Literal["onramp", "offramp"] | None = None
    status: Literal["success", "rejected"] = "success"

class BalanceUpdateData(BaseModel):
    event_type: Literal["balance_update"]
    payload: BalanceUpdatePayload

class LiquifyPositionPayload(BaseModel):
    user_id: str
    market: MARKET
    exit_price: float
    realized_pnl: float
    available: float = Field(ge=0)
    locked: float = Field(ge=0)

class LiquifyPositionData(BaseModel):
    event_type: Literal["liquify_position"]
    payload: LiquifyPositionPayload

class UnrealizedPnlPayload(BaseModel):
    pnl: float
    user_id: str
    market: MARKET

class UnrealizedPnlData(BaseModel):
    event_type: Literal["unrealized_pnl"]
    payload: UnrealizedPnlPayload

class OrderBookLevel(BaseModel):
    price: float
    qty: float

class OrderBookUpdatePayload(BaseModel):
    market: MARKET
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

class OrderBookDUpdateData(BaseModel):
    event_type: Literal["update_orderbook"]
    payload: OrderBookUpdatePayload
