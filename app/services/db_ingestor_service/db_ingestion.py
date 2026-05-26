from packages.db import SessionLocal
from packages.common_utils import (
    Position,     
    PositionCreatePayload,
    PositionClosePayload,
    BalanceUpdatePayload,
    LiquifyPositionPayload,)

from packages.models import (
    AccountBalance,
    FillProjection,
    OrderProjection,
    PositionProjection,
)

async def _get_or_create_balance(session, user_id: str):
    balance = await session.get(AccountBalance, user_id)
    if balance is None:
        balance = AccountBalance(user_id=user_id, available=0, locked=0)
        session.add(balance)
    return balance


def _apply_position(position_row: PositionProjection, position: Position):
    position_row.qty = position.qty
    position_row.entry_price = position.entry_price
    position_row.margin = position.margin
    position_row.liquidation_price = position.liquidation_price
    position_row.realized_pnl = position.realized_pnl


async def create_position(payload: PositionCreatePayload):
    async with SessionLocal() as session:
        order = await session.get(OrderProjection, payload.order_id)
        if order is None:
            order = OrderProjection(order_id=payload.order_id)
            session.add(order)
        order.user_id = payload.user_id
        order.market = payload.market
        order.status = payload.status
        order.remaining_qty = payload.remaining_qty

        for fill in payload.fills:
            existing_fill = await session.get(FillProjection, fill.fill_id)
            if existing_fill is not None:
                continue
            session.add(
                FillProjection(
                    fill_id=fill.fill_id,
                    market=fill.market,
                    maker_order_id=fill.maker_order_id,
                    taker_order_id=fill.taker_order_id,
                    maker_user_id=fill.maker_user_id,
                    taker_user_id=fill.taker_user_id,
                    price=fill.price,
                    qty=fill.qty,
                    timestamp_ms=fill.timestamp_ms,
                )
            )

        if payload.position is not None:
            position_key = {
                "user_id": payload.user_id,
                "market": payload.position.market,
            }
            position = await session.get(PositionProjection, position_key)
            if position is None:
                position = PositionProjection(**position_key)
                session.add(position)
            _apply_position(position, payload.position)

            balance = await _get_or_create_balance(session, payload.user_id)
            balance.locked = payload.position.margin

        await session.commit()


async def close_position(payload: PositionClosePayload):
    async with SessionLocal() as session:
        balance = await _get_or_create_balance(session, payload.user_id)
        balance.available = payload.available
        balance.locked = payload.locked

        position = await session.get(
            PositionProjection,
            {"user_id": payload.user_id, "market": payload.market},
        )
        if position is not None:
            await session.delete(position)

        await session.commit()


async def balance_update(payload: BalanceUpdatePayload):
    async with SessionLocal() as session:
        balance = await _get_or_create_balance(session, payload.user_id)
        balance.available = payload.balance
        await session.commit()


async def liquify_position(payload: LiquifyPositionPayload):
    async with SessionLocal() as session:
        balance = await _get_or_create_balance(session, payload.user_id)
        balance.available = payload.available
        balance.locked = payload.locked

        position = await session.get(
            PositionProjection,
            {"user_id": payload.user_id, "market": payload.market},
        )
        if position is not None:
            position.realized_pnl = payload.realized_pnl
            await session.delete(position)

        await session.commit()
