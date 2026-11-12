import heapq
import time

from sqlalchemy import select

from data_structures import (
    Collateral,
    OrderRecord,
    Position,
    User,
    order_books,
    orders,
    position_by_user,
    users,
)
from packages.db import SessionLocal
from packages.models import AccountBalance, OrderProjection, PositionProjection


async def restore_exchange_state() -> None:
    """Restore balances, open orders, and positions before consuming events."""
    async with SessionLocal() as session:
        balances = list(await session.scalars(select(AccountBalance)))
        positions = list(await session.scalars(select(PositionProjection)))
        open_orders = list(
            await session.scalars(
                select(OrderProjection)
                .where(
                    OrderProjection.status.in_(("open", "partially_filled")),
                    OrderProjection.remaining_qty > 0,
                )
                .order_by(OrderProjection.updated_at, OrderProjection.order_id)
            )
        )

    for balance in balances:
        users[balance.user_id] = User(
            user_id=balance.user_id,
            collateral=Collateral(
                available=balance.available,
                locked=balance.locked,
            ),
            wallet_balance=balance.wallet_balance,
        )
        position_by_user.setdefault(balance.user_id, {})

    for row in positions:
        user = users.setdefault(
            row.user_id,
            User(
                user_id=row.user_id,
                collateral=Collateral(available=0, locked=0),
            ),
        )
        position_by_user.setdefault(user.user_id, {})[row.market] = Position(
            market=row.market,
            qty=row.qty,
            entry_price=row.entry_price,
            margin=row.margin,
            liquidation_price=row.liquidation_price,
            unrealized_pnl=0,
            realized_pnl=row.realized_pnl,
        )

    for row in open_orders:
        # Older projections did not contain enough data to rebuild a book.
        # Leave those rows out rather than inventing a side or execution price.
        if row.side is None or row.order_type is None or row.price is None or row.leverage is None:
            continue
        user = users.setdefault(
            row.user_id,
            User(
                user_id=row.user_id,
                collateral=Collateral(available=0, locked=0),
            ),
        )
        position_by_user.setdefault(user.user_id, {})
        order = OrderRecord(
            user_id=row.user_id,
            order_id=row.order_id,
            market=row.market,
            side=row.side,
            qty=row.remaining_qty,
            margin=(row.remaining_qty * row.price) / row.leverage,
            leverage=row.leverage,
            order_type=row.order_type,
            price=row.price,
            status=row.status,
        )
        orders[row.order_id] = order
        timestamp = row.updated_at.timestamp() if row.updated_at else time.time()
        price = -row.price if row.side == "long" else row.price
        order_books[row.market][row.side].append((price, timestamp, row.order_id))

    for market in order_books.values():
        heapq.heapify(market["long"])
        heapq.heapify(market["short"])
