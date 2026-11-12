import os

from data_structures import User, users, Collateral, position_by_user
from packages.common_utils import BalanceDetails, STREAM_PERPS_UPDATES, BalanceUpdateData, BalanceUpdatePayload
from app import broker
from uuid import uuid4

DEFAULT_WALLET_BALANCE = float(os.getenv("DEMO_WALLET_BALANCE", "10000"))


def _get_or_create_user(user_id: str):
    if not users.get(user_id):
        users[user_id] = User(
            user_id=user_id,
            collateral=Collateral(available=0, locked=0),
            wallet_balance=DEFAULT_WALLET_BALANCE,
        )
        position_by_user[user_id] = {}
    return users[user_id]


def offramp(user_id: str, amount: float):
    user = _get_or_create_user(user_id)
    if user.collateral.available < amount:
        return BalanceUpdatePayload(
            message= "not enough balance to withdraw",
            user_id=user_id,
            balance=user.collateral.available,
            locked=user.collateral.locked,
            wallet_balance=user.wallet_balance,
            update_type="offramp",
            status="rejected",
        )
    user.collateral.available -= amount
    user.wallet_balance += amount
    return BalanceUpdatePayload(
        message="Balance updated successfully",
        balance=user.collateral.available,
        locked=user.collateral.locked,
        wallet_balance=user.wallet_balance,
        user_id=user_id,
        update_type="offramp",
        status="success",
    )

def onramp(user_id: str, amount: float):
    user = _get_or_create_user(user_id)
    if user.wallet_balance < amount:
        return BalanceUpdatePayload(
            message="not enough wallet balance to deposit",
            balance=user.collateral.available,
            locked=user.collateral.locked,
            wallet_balance=user.wallet_balance,
            user_id=user_id,
            update_type="onramp",
            status="rejected",
        )
    user.wallet_balance -= amount
    user.collateral.available += amount
    return BalanceUpdatePayload(
        message = "user balance updated",
        balance=user.collateral.available,
        locked=user.collateral.locked,
        wallet_balance=user.wallet_balance,
        user_id=user_id,
        update_type="onramp",
        status="success",
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
