from uuid import uuid4
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel, Field      
from packages.common_utils import (
    MARKET, 
    SIDE, 
    ORDER_TYPE, 
    MAX_LEVERAGE, 
    MIN_LEVERAGE, 
    OrderCreate, 
    OrderCreatePayload, 
    BalanceUpdate,
    BalanceDetails,
    STREAM_PERPS,
    ClosePosition,
    ClosePositionDetails
)
from app import broker
from security import require_auth

router = APIRouter(tags=["exchange_router"])
logger = logging.getLogger(__name__)

class Order(BaseModel):
    market: MARKET
    side: SIDE
    order_type: ORDER_TYPE
    qty: float
    price: float
    leverage: int = Field(le=MAX_LEVERAGE, ge=MIN_LEVERAGE)

@router.post("/order", status_code=status.HTTP_201_CREATED)
async def create_order(order: Order, user_id: str = Depends(require_auth)):
    order_id = str(uuid4())
    correlation_id = str(uuid4())

    payload = OrderCreatePayload(
        **order.model_dump(),
        user_id=user_id,
        order_id=order_id,
        margin=(order.qty * order.price) / order.leverage,
        status="open",
    )
    message = OrderCreate(
        event_type="order_create",
        payload=payload,
    )
    try:
        await broker.publish(
            message=message,
            stream=STREAM_PERPS,
            correlation_id=correlation_id,
        )
    except Exception as e:
        logger.exception(
            msg=f"failed to publish order event to broker",
            extra={
                "user_id": user_id,
                "order_id": order_id,
                "correlation_id": correlation_id,
                "error": str(e)
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="backend order service is temporarily unavailable",
        )
    return {
        "message": "accepted",
        "order_id": order_id,
        "correlation_id": correlation_id,
    }

class AmountRequest(BaseModel):
    amount: float = Field(gt=0)

@router.post("/onramp", status_code=status.HTTP_202_ACCEPTED)
async def onramp_route(payload: AmountRequest, user_id: str = Depends(require_auth)):
    # task = onramp.delay(user_id, payload.amount)
    # return TaskAccepted(task_id=task.id)
    balance_details = BalanceDetails(
        update_type="onramp",
        user_id=user_id,
        amount=payload.amount
    )
    message = BalanceUpdate(
        event_type="balance_update",
        payload=balance_details
    )
    correlation_id = str(uuid4())
    try:
        await broker.publish(
            message=message,
            stream=STREAM_PERPS,
            correlation_id=correlation_id
        )
    except Exception as e:
        logger.exception(
            msg="message to add balance redis stream failed",
            extra={
                "correlation_id": correlation_id,
                "user_id": user_id,
                "error": str(e)
            }
        )
    return message

@router.post("/offramp", status_code=status.HTTP_202_ACCEPTED)
async def offramp(payload: AmountRequest, user_id = Depends(require_auth)):
    balance_details = BalanceDetails(
        update_type="offramp",
        user_id=user_id,
        amount=payload.amount
    )
    message = BalanceUpdate(
        event_type="balance_update",
        payload=balance_details
    )
    correlation_id = str(uuid4())
    try:
        await broker.publish(
            message=message,
            stream=STREAM_PERPS,
            correlation_id=correlation_id
        )
    except Exception as e:
        logger.exception(
            msg="failed to send to redis stream from offramp",
            extra={
                "user_id": user_id,
                "correlation_id": correlation_id,
                "error": str(e)
            }
        )
    return message


@router.post("/close-position", status_code=status.HTTP_202_ACCEPTED)
async def close_position(market=Query(), user_id = Depends(require_auth)):
    payload = ClosePositionDetails(
        user_id=user_id,
        market=market
    )
    message= ClosePosition(
        event_type="close_position",
        payload=payload
    )
    correlation_id = str(uuid4())
    try:
        await broker.publish(
            message=message,
            stream=STREAM_PERPS,
            correlation_id=correlation_id
        )
    except Exception as e:
        logger.exception(
            msg="could not send the close position event to stream"
        )

