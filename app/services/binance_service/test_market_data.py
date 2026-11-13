from app import _extract_market_index_prices


def test_extracts_binance_futures_index_prices():
    assert _extract_market_index_prices(
        [
            {"s": "BTCUSDT", "i": "64000.0", "p": "64010.0"},
            {"s": "ETHUSDT", "i": "3200.0", "p": "3201.0"},
        ]
    ) == {"BTC-PERP": 64000.0, "ETH-PERP": 3200.0}


def test_extracts_public_spot_ticker_prices():
    assert _extract_market_index_prices(
        [
            {"symbol": "BTCUSDT", "price": "64000.0"},
            {"symbol": "ETHUSDT", "price": "3200.0"},
        ]
    ) == {"BTC-PERP": 64000.0, "ETH-PERP": 3200.0}


def test_ignores_unsupported_or_invalid_ticker_prices():
    assert _extract_market_index_prices(
        [
            {"symbol": "BNBUSDT", "price": "500"},
            {"symbol": "BTCUSDT", "price": "0"},
            {"symbol": "ETHUSDT", "price": "not-a-price"},
        ]
    ) == {}
