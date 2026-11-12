import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException, status
from sqlalchemy import select

from connection_manager import manager
from packages.common_utils import MARKET, PositionCreateData, PositionCreatePayload
from packages.db import SessionLocal
from packages.models import AccountBalance, PositionProjection
from security import decode_token


router = APIRouter(tags=["websocket"])

SUPPORTED_MARKETS = {"BTC-PERP", "ETH-PERP"}


async def _load_account_snapshot(user_id: str):
    async with SessionLocal() as session:
        balance = await session.get(AccountBalance, user_id)
        positions = await session.scalars(
            select(PositionProjection).where(PositionProjection.user_id == user_id)
        )
        return (
            {
                "balance": balance.available if balance else 0.0,
                "locked": balance.locked if balance else 0.0,
                "wallet_balance": (
                    balance.wallet_balance
                    if balance
                    else float(os.getenv("DEMO_WALLET_BALANCE", "10000"))
                ),
            },
            list(positions),
        )


async def _send_account_snapshot(websocket: WebSocket, user_id: str):
    balance, positions = await _load_account_snapshot(user_id)
    await websocket.send_json(
        {
            "event_type": "balance_update",
            "payload": {
                "user_id": user_id,
                "message": "account snapshot",
                "balance": balance["balance"],
                "locked": balance["locked"],
                "wallet_balance": balance["wallet_balance"],
                "status": "success",
            },
        }
    )
    for position in positions:
        payload = PositionCreatePayload(
            user_id=user_id,
            order_id="account-snapshot",
            market=position.market,
            status="filled",
            remaining_qty=0,
            fills=[],
            position={
                "market": position.market,
                "qty": position.qty,
                "entry_price": position.entry_price,
                "margin": position.margin,
                "liquidation_price": position.liquidation_price,
                "unrealized_pnl": 0,
                "realized_pnl": position.realized_pnl,
                "entry": position.entry_price,
                "side": "long" if position.qty > 0 else "short",
                "leverage": (
                    abs(position.qty) * position.entry_price / position.margin
                    if position.margin > 0
                    else 1
                ),
            },
            available=balance["balance"],
            locked=balance["locked"],
        )
        await websocket.send_json(
            PositionCreateData(event_type="create_position", payload=payload).model_dump()
        )


async def _handle_market_message(websocket: WebSocket, message: dict):
    message_type = message.get("type")
    if message_type == "subscribe_market":
        market = message.get("market")
        if market not in SUPPORTED_MARKETS:
            await websocket.send_json({
                "event_type": "error",
                "payload": {"message": "unsupported market"}
            })
            return
        manager.subscribe_market(websocket, market)
        await websocket.send_json({
            "event_type": "market_subscribed",
            "payload": {"market": market}
        })

    elif message_type == "unsubscribe_market":
        market = message.get("market")
        if market in SUPPORTED_MARKETS:
            manager.unsubscribe_market(websocket, market)
        await websocket.send_json({
            "event_type": "market_unsubscribed",
            "payload": {"market": market}
        })

    elif message_type == "ping":
        await websocket.send_json({
            "event_type": "pong",
            "payload": {}
        })

    else:
        await websocket.send_json({
            "event_type": "error",
            "payload": {"message": "unsupported websocket message type"}
        })


@router.websocket("/ws/market")
async def market_websocket_endpoint(websocket: WebSocket):
    await manager.connect_market(websocket)
    try:
        while True:
            message = await websocket.receive_json()
            await _handle_market_message(websocket, message)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        raise


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None):
    if not token:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    try:
        user_id = decode_token(token)
    except Exception:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    await manager.connect_user(user_id, websocket)
    try:
        await _send_account_snapshot(websocket, user_id)
        while True:
            message = await websocket.receive_json()
            await _handle_market_message(websocket, message)

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        raise
