from data_structures import User, users, Collateral, position_by_user
from packages.common_utils import BalanceDetails, STREAM_PERPS_UPDATES, BalanceUpdateData, BalanceUpdatePayload
from app import broker
from uuid import uuid4

def offramp(user_id: str, amount: float):
    user = users[user_id]
    if user.collateral.available < amount:
        return BalanceUpdatePayload(
            message= "not enough balance to withdraw",
            user_id=user_id,
            balance=user.collateral.available
        )
    user.collateral.available -= amount
    return BalanceUpdatePayload(
        message="Balance updated successfully",
        balance=user.collateral.available,
        user_id=user_id
    )

def onramp(user_id: str, amount: float):
    if not users.get(user_id):  
        users[user_id] = User(user_id=user_id, collateral=Collateral(available=amount, locked=0))
        position_by_user[user_id] = {}
        return BalanceUpdatePayload(
            message="user created and balance updated",
            balance=amount,
            user_id=user_id
        )
    user = users[user_id]
    user.collateral.available += amount
    return BalanceUpdatePayload(
        message = "user balance updated",
        balance=user.collateral.available,
        user_id=user_id
    )

async def balance_update(payload: BalanceDetails):
    response: BalanceUpdatePayload = None
    if payload.update_type == "onramp":
        response = onramp(payload.user_id, payload.amount)
    elif payload.update_type == "offramp":
        response = offramp(payload.user_id, payload.amount)
    message = BalanceUpdateData(
        event_type="balance_update",
        payload=response
    )
    await broker.publish(
        message=message,
        stream=STREAM_PERPS_UPDATES,
        correlation_id=str(uuid4())
    )
