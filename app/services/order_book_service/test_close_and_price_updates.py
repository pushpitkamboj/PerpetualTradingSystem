import asyncio
import sys
import types
from pathlib import Path


ROOT_APP_DIR = Path(__file__).resolve().parents[2]
SERVICE_DIR = Path(__file__).resolve().parent

for path in (str(SERVICE_DIR), str(ROOT_APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


class DummyBroker:
    def __init__(self):
        self.messages = []

    async def publish(self, message, stream, correlation_id):
        self.messages.append(
            {
                "message": message,
                "stream": stream,
                "correlation_id": correlation_id,
            }
        )


dummy_broker = DummyBroker()
import app as root_app

root_app.broker = dummy_broker

from close_position import close_position
from create_order import create_order_engine, place_order_on_orderbook
from data_structures import Collateral, OrderRecord, Position, User, fills, order_books, orders, position_by_user, synthetic_orders, users
from on_price_update import on_price_update
from state_recovery import restore_exchange_state
from packages.db import Base
from packages.models import AccountBalance, OrderProjection, PositionProjection
from packages.common_utils import ClosePositionDetails, OrderCreatePayload
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _reset_state():
    users.clear()
    orders.clear()
    fills.clear()
    synthetic_orders.clear()
    position_by_user.clear()
    for market in order_books.values():
        market["long"].clear()
        market["short"].clear()
    dummy_broker.messages.clear()


def _seed_user(user_id: str, *, available: float = 1000, locked: float = 0):
    users[user_id] = User(
        user_id=user_id,
        collateral=Collateral(available=available, locked=locked),
    )
    position_by_user[user_id] = {}


def _seed_position(user_id: str, *, market: str = "BTC-PERP", qty: float = 1, entry_price: float = 100, margin: float = 50):
    position_by_user[user_id][market] = Position(
        market=market,
        qty=qty,
        entry_price=entry_price,
        margin=margin,
        liquidation_price=50 if qty > 0 else 150,
        unrealized_pnl=0,
        realized_pnl=None,
    )


def _seed_book_order(order_id: str, *, user_id: str, side: str, qty: float, price: float, leverage: float = 2):
    orders[order_id] = OrderRecord(
        user_id=user_id,
        order_id=order_id,
        market="BTC-PERP",
        side=side,
        qty=qty,
        margin=(qty * price) / leverage,
        leverage=leverage,
        order_type="limit",
        price=price,
        status="open",
    )
    place_order_on_orderbook(order_id)


def test_close_position_gracefully_rejects_when_book_has_no_opposing_depth():
    _reset_state()
    _seed_user("owner", available=950, locked=50)
    _seed_position("owner", qty=1, entry_price=100, margin=50)

    response = asyncio.run(
        close_position(ClosePositionDetails(user_id="owner", market="BTC-PERP"))
    )

    assert response == {"message": "not enough market liquidity to close position"}
    assert position_by_user["owner"]["BTC-PERP"] is not None
    assert users["owner"].collateral.available == 950
    assert users["owner"].collateral.locked == 50
    assert dummy_broker.messages == []


def test_close_position_does_not_consume_partial_depth():
    _reset_state()
    _seed_user("owner", available=950, locked=50)
    _seed_user("maker")
    _seed_position("owner", qty=1, entry_price=100, margin=50)
    _seed_book_order("maker-order", user_id="maker", side="long", qty=0.5, price=100)

    response = asyncio.run(
        close_position(ClosePositionDetails(user_id="owner", market="BTC-PERP"))
    )

    assert response == {"message": "not enough market liquidity to close position"}
    assert orders["maker-order"].qty == 0.5
    assert orders["maker-order"].status == "open"
    assert position_by_user["owner"]["BTC-PERP"].qty == 1
    assert users["owner"].collateral.available == 950
    assert users["owner"].collateral.locked == 50
    assert dummy_broker.messages == []


def test_close_position_uses_average_fill_price_across_book_levels():
    _reset_state()
    _seed_user("owner", available=950, locked=50)
    _seed_user("maker-a")
    _seed_user("maker-b")
    _seed_position("owner", qty=1, entry_price=100, margin=50)
    _seed_book_order("maker-a-order", user_id="maker-a", side="long", qty=0.5, price=110)
    _seed_book_order("maker-b-order", user_id="maker-b", side="long", qty=0.5, price=100)

    response = asyncio.run(
        close_position(ClosePositionDetails(user_id="owner", market="BTC-PERP"))
    )

    assert response.payload.exit_price == 105
    assert response.payload.realized_pnl == 5
    assert "BTC-PERP" not in position_by_user["owner"]
    assert position_by_user["maker-a"]["BTC-PERP"].qty == 0.5
    assert position_by_user["maker-b"]["BTC-PERP"].qty == 0.5
    assert {item["message"].event_type for item in dummy_broker.messages} == {
        "update_orderbook",
        "create_position",
        "close_position",
    }


def test_gap_loss_is_capped_at_isolated_margin():
    _reset_state()
    _seed_user("owner", available=0, locked=50)
    _seed_user("maker")
    _seed_position("owner", qty=1, entry_price=100, margin=50)
    _seed_book_order("maker-order", user_id="maker", side="long", qty=1, price=35)

    response = asyncio.run(
        close_position(ClosePositionDetails(user_id="owner", market="BTC-PERP"))
    )

    assert response.payload.exit_price == 35
    assert response.payload.realized_pnl == -50
    assert response.payload.available == 0
    assert response.payload.locked == 0


def test_unfunded_order_gets_a_terminal_rejection_event():
    _reset_state()
    order = OrderCreatePayload(
        user_id="new-user",
        order_id="unfunded-order",
        market="BTC-PERP",
        side="long",
        qty=1,
        margin=100,
        leverage=1,
        order_type="limit",
        price=100,
        status="open",
    )

    response = asyncio.run(create_order_engine(order))

    assert response.status == "rejected"
    assert response.order_id == "unfunded-order"
    assert "new-user" in users
    assert users["new-user"].collateral.available == 0
    assert len(dummy_broker.messages) == 1
    assert dummy_broker.messages[0]["message"].payload.status == "rejected"


def test_crossing_orders_create_positions_for_both_traders():
    _reset_state()
    _seed_user("buyer", available=1000)
    _seed_user("seller", available=1000)
    buy = OrderCreatePayload(
        user_id="buyer", order_id="buy-order", market="BTC-PERP", side="long",
        qty=1, margin=50, leverage=2, order_type="limit", price=100, status="open",
    )
    sell = OrderCreatePayload(
        user_id="seller", order_id="sell-order", market="BTC-PERP", side="short",
        qty=1, margin=50, leverage=2, order_type="limit", price=100, status="open",
    )

    asyncio.run(create_order_engine(buy))
    asyncio.run(create_order_engine(sell))

    assert position_by_user["buyer"]["BTC-PERP"].qty == 1
    assert position_by_user["seller"]["BTC-PERP"].qty == -1
    assert users["buyer"].collateral.available == 950
    assert users["seller"].collateral.available == 950
    position_events = [
        item["message"].payload
        for item in dummy_broker.messages
        if item["message"].event_type == "create_position"
    ]
    assert {event.user_id for event in position_events} == {"buyer", "seller"}
    assert all(event.available == 950 and event.locked == 50 for event in position_events)
    assert len(fills) == 1


def test_execution_price_margin_is_reserved_before_matching():
    _reset_state()
    _seed_user("buyer", available=1000)
    _seed_user("seller", available=110)
    _seed_book_order("bid", user_id="buyer", side="long", qty=1, price=110)
    sell = OrderCreatePayload(
        user_id="seller", order_id="sell-order", market="BTC-PERP", side="short",
        qty=1, margin=100, leverage=1, order_type="limit", price=100, status="open",
    )

    asyncio.run(create_order_engine(sell))

    assert position_by_user["seller"]["BTC-PERP"].qty == -1
    assert position_by_user["seller"]["BTC-PERP"].margin == 110
    assert users["seller"].collateral.available == 0
    assert users["seller"].collateral.locked == 110


def test_order_is_cancelled_if_actual_fill_margin_is_unaffordable():
    _reset_state()
    _seed_user("buyer", available=1000)
    _seed_user("seller", available=100)
    _seed_book_order("bid", user_id="buyer", side="long", qty=1, price=110)
    sell = OrderCreatePayload(
        user_id="seller", order_id="sell-order", market="BTC-PERP", side="short",
        qty=1, margin=100, leverage=1, order_type="limit", price=100, status="open",
    )

    response = asyncio.run(create_order_engine(sell))

    assert response.status == "cancelled"
    assert users["seller"].collateral.available == 100
    assert users["seller"].collateral.locked == 0
    assert orders["bid"].qty == 1
    assert fills == {}


def test_market_order_cancels_unfilled_remainder_without_resting():
    _reset_state()
    _seed_user("trader", available=1000)
    order = OrderCreatePayload(
        user_id="trader", order_id="market-order", market="BTC-PERP", side="long",
        qty=1, margin=100, leverage=1, order_type="market", price=100, status="open",
    )

    response = asyncio.run(create_order_engine(order))

    assert response.status == "cancelled"
    assert users["trader"].collateral.available == 1000
    assert users["trader"].collateral.locked == 0
    assert order_books["BTC-PERP"]["long"] == []


def test_partially_filled_market_order_refunds_and_cancels_remainder():
    _reset_state()
    _seed_user("trader", available=1000)
    _seed_user("maker", available=1000)
    _seed_book_order("ask", user_id="maker", side="short", qty=1, price=100)
    order = OrderCreatePayload(
        user_id="trader", order_id="market-order", market="BTC-PERP", side="long",
        qty=2, margin=200, leverage=1, order_type="market", price=100, status="open",
    )

    response = asyncio.run(create_order_engine(order))

    assert response.status == "partially_filled_cancelled"
    assert response.remaining_qty == 1
    assert position_by_user["trader"]["BTC-PERP"].qty == 1
    assert users["trader"].collateral.available == 900
    assert users["trader"].collateral.locked == 100
    assert order_books["BTC-PERP"]["long"] == []


def test_self_trade_is_cancelled_without_mutating_the_resting_order():
    _reset_state()
    _seed_user("trader", available=1000)
    _seed_book_order("resting-ask", user_id="trader", side="short", qty=1, price=100)
    incoming_bid = OrderCreatePayload(
        user_id="trader", order_id="incoming-bid", market="BTC-PERP", side="long",
        qty=1, margin=100, leverage=1, order_type="limit", price=100, status="open",
    )

    response = asyncio.run(create_order_engine(incoming_bid))

    assert response.status == "cancelled"
    assert orders["resting-ask"].qty == 1
    assert orders["resting-ask"].status == "open"
    assert "BTC-PERP" not in position_by_user["trader"]
    assert users["trader"].collateral.available == 1000
    assert users["trader"].collateral.locked == 0


def test_engine_restart_restores_balances_positions_and_open_orders(monkeypatch):
    async def run_test():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        import state_recovery

        monkeypatch.setattr(state_recovery, "SessionLocal", session_factory)
        try:
            async with session_factory() as session:
                session.add(
                    AccountBalance(
                        user_id="restored-user",
                        available=700,
                        locked=300,
                        wallet_balance=9000,
                    )
                )
                session.add(
                    PositionProjection(
                        user_id="restored-user",
                        market="BTC-PERP",
                        qty=1,
                        entry_price=100,
                        margin=100,
                        liquidation_price=0,
                    )
                )
                session.add(
                    OrderProjection(
                        order_id="restored-order",
                        user_id="restored-user",
                        market="ETH-PERP",
                        status="open",
                        remaining_qty=2,
                        side="short",
                        order_type="limit",
                        price=200,
                        leverage=2,
                    )
                )
                await session.commit()

            await restore_exchange_state()

            assert users["restored-user"].collateral.available == 700
            assert users["restored-user"].collateral.locked == 300
            assert users["restored-user"].wallet_balance == 9000
            assert position_by_user["restored-user"]["BTC-PERP"].qty == 1
            assert orders["restored-order"].qty == 2
            assert order_books["ETH-PERP"]["short"][0][2] == "restored-order"
        finally:
            await engine.dispose()

    _reset_state()
    asyncio.run(run_test())


def test_on_price_update_publishes_unrealized_pnl_for_each_open_position():
    _reset_state()
    _seed_user("u1")
    _seed_user("u2")
    _seed_position("u1", qty=1, entry_price=100, margin=50)
    _seed_position("u2", qty=-2, entry_price=200, margin=100)

    result = asyncio.run(on_price_update({"BTC-PERP": 120}))

    unrealized = [
        item["message"]
        for item in dummy_broker.messages
        if getattr(item["message"], "event_type", None) == "unrealized_pnl"
    ]

    assert result == 160
    assert len(unrealized) == 2
    assert {(message.payload.user_id, message.payload.pnl) for message in unrealized} == {
        ("u1", 20),
        ("u2", 160),
    }
