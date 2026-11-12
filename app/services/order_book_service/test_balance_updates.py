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

from balance_updates import onramp, offramp
from data_structures import users, position_by_user


def _reset_state():
    users.clear()
    position_by_user.clear()


def test_onramp_increases_available_collateral():
    _reset_state()

    payload = onramp("u1", 2500)

    assert payload.status == "success"
    assert payload.update_type == "onramp"
    assert payload.balance == 2500
    assert payload.locked == 0
    assert users["u1"].collateral.available == 2500


def test_onramp_accumulates_collateral():
    _reset_state()
    onramp("u1", 9000)

    payload = onramp("u1", 1000)

    assert payload.status == "success"
    assert payload.update_type == "onramp"
    assert payload.balance == 10000
    assert users["u1"].collateral.available == 10000


def test_offramp_rejects_when_available_collateral_is_insufficient():
    _reset_state()
    onramp("u1", 1000)

    payload = offramp("u1", 1500)

    assert payload.status == "rejected"
    assert payload.update_type == "offramp"
    assert payload.balance == 1000
    assert users["u1"].collateral.available == 1000
