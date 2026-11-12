import math
import sys
import types
from pathlib import Path


ROOT_APP_DIR = Path(__file__).resolve().parents[2]
SERVICE_DIR = Path(__file__).resolve().parent

for path in (str(SERVICE_DIR), str(ROOT_APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import app as root_app

root_app.broker = types.SimpleNamespace()

from create_order import calculate_position_from_fill
from data_structures import Collateral, Fill, Position, User, fills, orders, position_by_user, synthetic_orders, users


def _reset_state():
    users.clear()
    position_by_user.clear()
    orders.clear()
    fills.clear()
    synthetic_orders.clear()


def _seed_user(user_id: str, *, available: float, locked: float):
    users[user_id] = User(
        user_id=user_id,
        collateral=Collateral(available=available, locked=locked),
    )
    position_by_user[user_id] = {}


def _seed_position(
    user_id: str,
    *,
    qty: float,
    entry_price: float,
    margin: float,
    liquidation_price: float,
    unrealized_pnl: float = 0,
):
    pos = Position(
        market="BTC-PERP",
        qty=qty,
        entry_price=entry_price,
        margin=margin,
        liquidation_price=liquidation_price,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=None,
    )
    position_by_user[user_id]["BTC-PERP"] = pos
    return pos


def _fill(qty: float, price: float):
    return Fill(
        fill_id="fill-1",
        market="BTC-PERP",
        maker_order_id="maker-order",
        taker_order_id="taker-order",
        maker_user_id="maker-user",
        taker_user_id="taker-user",
        price=price,
        qty=qty,
        timestamp_ms=1.0,
    )


def test_same_direction_long_increase_updates_weighted_entry_and_margin():
    _reset_state()
    _seed_user("u1", available=900, locked=100)
    pos = _seed_position("u1", qty=1, entry_price=100, margin=50, liquidation_price=50)

    calculate_position_from_fill(pos, _fill(qty=0.5, price=200), "long", 2, "u1")

    updated = position_by_user["u1"]["BTC-PERP"]
    assert math.isclose(updated.qty, 1.5)
    assert math.isclose(updated.entry_price, (100 + 100) / 1.5)
    assert math.isclose(updated.margin, 100)
    assert math.isclose(updated.liquidation_price, updated.entry_price - (100 / 1.5))
    assert users["u1"].collateral.available == 900
    assert users["u1"].collateral.locked == 100


def test_exact_close_releases_all_locked_margin_and_realizes_pnl():
    _reset_state()
    _seed_user("u1", available=890, locked=110)
    pos = _seed_position("u1", qty=1, entry_price=100, margin=50, liquidation_price=50)

    calculate_position_from_fill(pos, _fill(qty=1, price=120), "short", 2, "u1")

    assert position_by_user["u1"]["BTC-PERP"] is None
    assert math.isclose(pos.realized_pnl, 20)
    assert math.isclose(users["u1"].collateral.available, 1020)
    assert math.isclose(users["u1"].collateral.locked, 0)


def test_reduction_uses_quantity_not_equal_notional_and_releases_proportionally():
    _reset_state()
    _seed_user("u1", available=900, locked=100)
    pos = _seed_position("u1", qty=1, entry_price=100, margin=50, liquidation_price=50)

    calculate_position_from_fill(pos, _fill(qty=0.5, price=200), "short", 2, "u1")

    updated = position_by_user["u1"]["BTC-PERP"]
    assert math.isclose(updated.qty, 0.5)
    assert math.isclose(updated.entry_price, 100)
    assert math.isclose(updated.margin, 25)
    assert math.isclose(updated.realized_pnl, 50)
    assert math.isclose(updated.liquidation_price, 100 - (25 / 0.5))
    assert math.isclose(users["u1"].collateral.available, 1025)
    assert math.isclose(users["u1"].collateral.locked, 25)


def test_flip_reopens_residual_in_new_direction_and_keeps_only_new_margin_locked():
    _reset_state()
    _seed_user("u1", available=800, locked=200)
    pos = _seed_position("u1", qty=1, entry_price=100, margin=50, liquidation_price=50)

    calculate_position_from_fill(pos, _fill(qty=1.5, price=200), "short", 2, "u1")

    updated = position_by_user["u1"]["BTC-PERP"]
    assert math.isclose(updated.qty, -0.5)
    assert math.isclose(updated.entry_price, 200)
    assert math.isclose(updated.margin, 50)
    assert math.isclose(updated.realized_pnl, 100)
    assert math.isclose(updated.liquidation_price, 200 + (50 / 0.5))
    assert math.isclose(users["u1"].collateral.available, 1050)
    assert math.isclose(users["u1"].collateral.locked, 50)


def test_short_reduction_realizes_pnl_and_preserves_short_entry_price():
    _reset_state()
    _seed_user("u1", available=875, locked=125)
    pos = _seed_position("u1", qty=-1, entry_price=200, margin=100, liquidation_price=300)

    calculate_position_from_fill(pos, _fill(qty=0.5, price=100), "long", 2, "u1")

    updated = position_by_user["u1"]["BTC-PERP"]
    assert math.isclose(updated.qty, -0.5)
    assert math.isclose(updated.entry_price, 200)
    assert math.isclose(updated.margin, 50)
    assert math.isclose(updated.realized_pnl, 50)
    assert math.isclose(updated.liquidation_price, 200 + (50 / 0.5))
    assert math.isclose(users["u1"].collateral.available, 1000)
    assert math.isclose(users["u1"].collateral.locked, 50)


def test_short_flip_reopens_long_residual_with_new_entry_price():
    _reset_state()
    _seed_user("u1", available=825, locked=175)
    pos = _seed_position("u1", qty=-1, entry_price=200, margin=100, liquidation_price=300)

    calculate_position_from_fill(pos, _fill(qty=1.5, price=100), "long", 2, "u1")

    updated = position_by_user["u1"]["BTC-PERP"]
    assert math.isclose(updated.qty, 0.5)
    assert math.isclose(updated.entry_price, 100)
    assert math.isclose(updated.margin, 25)
    assert math.isclose(updated.realized_pnl, 100)
    assert math.isclose(updated.liquidation_price, 100 - (25 / 0.5))
    assert math.isclose(users["u1"].collateral.available, 1075)
    assert math.isclose(users["u1"].collateral.locked, 25)
