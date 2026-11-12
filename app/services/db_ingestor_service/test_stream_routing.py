import asyncio
import importlib.util
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import create_engine as create_sync_engine, inspect as inspect_sync


ROOT_APP_DIR = Path(__file__).resolve().parents[2]
SERVICE_DIR = Path(__file__).resolve().parent

for path in (str(SERVICE_DIR), str(ROOT_APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


app_spec = importlib.util.spec_from_file_location(
    "db_ingestor_app",
    SERVICE_DIR / "app.py",
)
db_ingestor_app = importlib.util.module_from_spec(app_spec)
sys.modules[app_spec.name] = db_ingestor_app
app_spec.loader.exec_module(db_ingestor_app)
handle_perps_updates = db_ingestor_app.handle_perps_updates
from packages.common_utils import BinancePrice
from packages.db import Base, _migrate_account_balance_wallet
from packages.models import AccountBalance, PositionProjection
import db_ingestion


def test_handle_perps_updates_ignores_binance_price_events():
    asyncio.run(
        handle_perps_updates(
            BinancePrice(event_type="binance_data", payload={"BTC-PERP": 250.0})
        )
    )


def test_position_event_persists_collateral_even_when_position_is_closed():
    async def run_test():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        original_session_factory = db_ingestion.SessionLocal
        db_ingestion.SessionLocal = session_factory
        try:
            from packages.common_utils import BalanceUpdatePayload, PositionCreatePayload

            async with session_factory() as session:
                session.add(
                    PositionProjection(
                        user_id="maker",
                        market="BTC-PERP",
                        qty=1,
                        entry_price=100,
                        margin=50,
                        liquidation_price=50,
                    )
                )
                await session.commit()

            await db_ingestion.create_position(
                PositionCreatePayload(
                    user_id="maker",
                    order_id="maker-order",
                    market="BTC-PERP",
                    status="filled",
                    remaining_qty=0,
                    fills=[],
                    position=None,
                    available=125.0,
                    locked=0.0,
                )
            )
            await db_ingestion.balance_update(
                BalanceUpdatePayload(
                    user_id="maker",
                    message="deposit complete",
                    balance=125.0,
                    locked=0.0,
                    wallet_balance=9875.0,
                    update_type="onramp",
                    status="success",
                )
            )
            async with session_factory() as session:
                balance = await session.get(AccountBalance, "maker")
                assert balance is not None
                assert balance.available == 125.0
                assert balance.locked == 0.0
                assert balance.wallet_balance == 9875.0
                assert await session.get(
                    PositionProjection,
                    {"user_id": "maker", "market": "BTC-PERP"},
                ) is None
        finally:
            db_ingestion.SessionLocal = original_session_factory
            await engine.dispose()

    asyncio.run(run_test())


def test_account_balance_migration_adds_wallet_to_existing_database():
    engine = create_sync_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE account_balances ("
                "user_id VARCHAR PRIMARY KEY, available FLOAT NOT NULL, locked FLOAT NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO account_balances (user_id, available, locked) "
                "VALUES ('existing-user', 20, 5)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE order_projections ("
                "order_id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, "
                "market VARCHAR NOT NULL, status VARCHAR NOT NULL, "
                "remaining_qty FLOAT NOT NULL, updated_at DATETIME)"
            )

            _migrate_account_balance_wallet(connection)
            row = connection.exec_driver_sql(
                "SELECT available, locked, wallet_balance "
                "FROM account_balances WHERE user_id='existing-user'"
            ).one()

        assert row == (20, 5, 10_000)
        order_columns = {
            column["name"]
            for column in inspect_sync(engine).get_columns("order_projections")
        }
        assert {"side", "order_type", "price", "leverage"}.issubset(order_columns)
    finally:
        engine.dispose()
