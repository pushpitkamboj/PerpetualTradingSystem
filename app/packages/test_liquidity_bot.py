from packages.liquidity_bot import LiquidityBot, MarketView


def test_liquidity_bot_places_resting_quotes_on_each_side_of_mid():
    bot = LiquidityBot()
    view = MarketView(price=64_000)
    try:
        long_quotes = [bot.best_working_price(view, "long") for _ in range(100)]
        short_quotes = [bot.best_working_price(view, "short") for _ in range(100)]
    finally:
        import asyncio

        asyncio.run(bot.client.aclose())

    assert all(63_974.4 <= price < 64_000 for price in long_quotes)
    assert all(64_000 < price <= 64_025.6 for price in short_quotes)
