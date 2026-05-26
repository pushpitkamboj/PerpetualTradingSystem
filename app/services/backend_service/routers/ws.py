from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException, status

from connection_manager import manager
from packages.common_utils import MARKET
from security import decode_token


router = APIRouter(tags=["websocket"])

SUPPORTED_MARKETS = {"BTC-PERP", "ETH-PERP"}
 

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
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "subscribe_market":
                market = message.get("market")
                if market not in SUPPORTED_MARKETS:
                    await websocket.send_json({
                        "event_type": "error",
                        "payload": {"message": "unsupported market"}
                    })
                    continue
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

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
        raise
