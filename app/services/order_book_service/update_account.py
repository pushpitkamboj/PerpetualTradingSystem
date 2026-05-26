from data_structures import Position, position_by_user, users


def update_account_after_close_position(user_id, exit_price, pos: Position):
    user = users[user_id]
    collateral = user.collateral
    realized_pnl = 0
    if pos.qty > 0:
        realized_pnl = (exit_price - pos.entry_price) * abs(pos.qty)
    else:
        realized_pnl = (pos.entry_price - exit_price) * abs(pos.qty)
    pos.realized_pnl = realized_pnl
    collateral.available += pos.margin + realized_pnl
    collateral.locked -= pos.margin
    position_by_user[user_id].pop(pos.market, None)
    return realized_pnl, collateral.available, collateral.locked