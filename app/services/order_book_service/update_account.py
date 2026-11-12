from data_structures import Position, position_by_user, users


def update_account_after_close_position(user_id, exit_price, pos: Position):
    user = users[user_id]
    collateral = user.collateral
    realized_pnl = 0
    if pos.qty > 0:
        realized_pnl = (exit_price - pos.entry_price) * abs(pos.qty)
    else:
        realized_pnl = (pos.entry_price - exit_price) * abs(pos.qty)
    # This engine uses isolated margin and has no insurance-fund or ADL layer.
    # Cap gap losses at the position's allocated margin so account collateral
    # and the nonnegative balance event contract stay consistent.
    realized_pnl = max(realized_pnl, -pos.margin)
    pos.realized_pnl = realized_pnl
    collateral.available = max(0.0, collateral.available + pos.margin + realized_pnl)
    collateral.locked = max(0.0, collateral.locked - pos.margin)
    position_by_user[user_id].pop(pos.market, None)
    return realized_pnl, collateral.available, collateral.locked
