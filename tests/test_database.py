import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ripple_tradePilot.storage import database_path, init_database
from ripple_tradePilot.storage.__main__ import main as initialize_storage
from ripple_tradePilot.storage.database import (
    DATABASE_SCHEMA_VERSION,
    list_stock_catalog,
    load_daily_bars,
    upsert_daily_bars,
    upsert_stock_catalog,
    upsert_stock_quotes,
)


class DatabaseInitializationTest(unittest.TestCase):
    def test_creates_database_table_and_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "db" / "tradepilot.db"

            initialized = init_database(target)

            self.assertEqual(initialized, target)
            with sqlite3.connect(target) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(backtest_results)")
                }
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(backtest_results)")
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertIn("annual_return", columns)
            self.assertIn("created_at", columns)
            self.assertIn("user_id", columns)
            self.assertIn("strategy_id", columns)
            self.assertIn("idx_backtest_results_symbol_created", indexes)
            self.assertTrue(
                {
                    "users",
                    "user_sessions",
                    "strategies",
                    "user_watchlist",
                    "stock_catalog",
                    "daily_bars",
                    "stock_quotes",
                }.issubset(tables)
            )
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_storage_startup_command_migrates_market_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-market.db"
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "CREATE TABLE stock_catalog ("
                    "symbol TEXT PRIMARY KEY, name TEXT, market TEXT)"
                )
                connection.execute(
                    "CREATE TABLE daily_bars (id INTEGER PRIMARY KEY)"
                )

            output = StringIO()
            with (
                patch.dict(
                    os.environ, {"TRADEPILOT_BACKTEST_DB": str(target)}
                ),
                redirect_stdout(output),
            ):
                initialize_storage()

            with sqlite3.connect(target) as connection:
                catalog_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(stock_catalog)")
                }
                daily_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(daily_bars)")
                }
                quote_table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'stock_quotes'"
                ).fetchone()
                watchlist_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(user_watchlist)")
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertTrue(
                {"exchange", "board", "industry", "area"}.issubset(
                    catalog_columns
                )
            )
            self.assertTrue(
                {"pre_close", "change", "pct_chg"}.issubset(daily_columns)
            )
            self.assertIsNotNone(quote_table)
            self.assertIn("default_strategy_id", watchlist_columns)
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)
            self.assertIn(
                f"SQLite schema v{DATABASE_SCHEMA_VERSION} ready", output.getvalue()
            )

    def test_adds_columns_missing_from_legacy_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy.db"
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "CREATE TABLE backtest_results (id INTEGER PRIMARY KEY, symbol TEXT)"
                )

            init_database(target)

            with sqlite3.connect(target) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(backtest_results)")
                }
            self.assertTrue({"name", "total_return", "created_at"}.issubset(columns))

    def test_environment_path_uses_configured_database(self):
        configured = "/tmp/tradepilot-test.db"
        with patch.dict(os.environ, {"TRADEPILOT_BACKTEST_DB": configured}):
            self.assertEqual(database_path(), Path(configured))

    def test_repairs_missing_columns_in_existing_user_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy-users.db"
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE backtest_results (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
                connection.execute("INSERT INTO users (id) VALUES (7)")
                connection.execute("CREATE TABLE user_sessions (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE strategies (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE user_watchlist (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE daily_bars (id INTEGER PRIMARY KEY)")

            init_database(target)

            with sqlite3.connect(target) as connection:
                user_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(users)")
                }
                session_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(user_sessions)")
                }
                strategy_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(strategies)")
                }
                migrated_role = connection.execute(
                    "SELECT role FROM users WHERE id = 7"
                ).fetchone()[0]
                watchlist_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(user_watchlist)")
                }
                daily_bar_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(daily_bars)")
                }
            self.assertTrue({"username", "password_hash", "role", "created_at"}.issubset(user_columns))
            self.assertTrue({"user_id", "token_hash", "expires_at"}.issubset(session_columns))
            self.assertTrue(
                {"user_id", "visibility", "parameters_json", "system_key"}.issubset(
                    strategy_columns
                )
            )
            self.assertEqual(migrated_role, "admin")
            self.assertTrue(
                {
                    "user_id",
                    "symbol",
                    "is_watched",
                    "last_updated_at",
                    "default_strategy_id",
                }.issubset(
                    watchlist_columns
                )
            )
            self.assertTrue(
                {
                    "symbol",
                    "trade_date",
                    "close",
                    "pre_close",
                    "change",
                    "pct_chg",
                    "source",
                }.issubset(daily_bar_columns)
            )

    def test_daily_bars_are_upserted_by_symbol_and_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            first = {
                "trade_date": "20260831",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "pre_close": 10.2,
                "change": 0.3,
                "pct_chg": 2.9412,
                "vol": 100,
            }
            upsert_daily_bars("600000.SH", [first], "test", target)
            upsert_daily_bars(
                "600000.SH", [{**first, "close": 10.8, "vol": 120}], "test", target
            )

            rows = load_daily_bars("600000.SH", target)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["close"], 10.8)
            self.assertEqual(rows[0]["vol"], 120)
            self.assertEqual(rows[0]["pre_close"], 10.2)
            self.assertEqual(rows[0]["change"], 0.3)
            self.assertEqual(rows[0]["pct_chg"], 2.9412)

    def test_stock_catalog_upsert_updates_watchlist_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                    ("catalog-user", "hash"),
                )
                user_id = connection.execute(
                    "SELECT id FROM users WHERE username = ?", ("catalog-user",)
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO user_watchlist (user_id, symbol, name) VALUES (?, ?, ?)",
                    (user_id, "600418.SH", "江淮汽车"),
                )

            upsert_stock_catalog(
                [
                    {
                        "symbol": "600418.SH",
                        "name": "ST江淮",
                        "market": "主板",
                        "exchange": "SSE",
                        "board": "主板",
                        "industry": "汽车整车",
                        "area": "安徽",
                        "list_status": "L",
                        "list_date": "20010930",
                    }
                ],
                "tushare",
                target,
            )

            with sqlite3.connect(target) as connection:
                name = connection.execute(
                    "SELECT name FROM user_watchlist WHERE symbol = ?",
                    ("600418.SH",),
                ).fetchone()[0]
            catalog = list_stock_catalog(target)

            self.assertEqual(name, "ST江淮")
            self.assertEqual(catalog[0]["market"], "主板")
            self.assertEqual(catalog[0]["exchange"], "SSE")
            self.assertEqual(catalog[0]["board"], "主板")
            self.assertEqual(catalog[0]["industry"], "汽车整车")
            self.assertEqual(catalog[0]["area"], "安徽")

    def test_realtime_quote_takes_priority_over_official_daily_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600000.SH", "name": "浦发银行"}], "test", target
            )
            upsert_daily_bars(
                "600000.SH",
                [
                    {
                        "trade_date": "20260901",
                        "open": 10,
                        "high": 10.5,
                        "low": 9.8,
                        "close": 10.2,
                        "pre_close": 10,
                        "change": 0.2,
                        "pct_chg": 2,
                        "vol": 100,
                    }
                ],
                "tushare",
                target,
            )
            upsert_stock_quotes(
                [
                    {
                        "symbol": "600000.SH",
                        "price": 10.6,
                        "pre_close": 10.2,
                        "change": 0.4,
                        "change_pct": 3.9216,
                        "quote_time": "2026-09-02T10:30:00",
                    }
                ],
                "akshare",
                target,
            )

            item = list_stock_catalog(target)[0]

            self.assertEqual(item["price"], 10.6)
            self.assertEqual(item["change"], 0.4)
            self.assertEqual(item["change_pct"], 3.9216)
            self.assertEqual(item["price_kind"], "realtime")
            self.assertEqual(item["price_source"], "akshare")
            self.assertEqual(item["price_time"], "2026-09-02T10:30:00")

    def test_daily_catalog_uses_official_change_fields_on_adjustment_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600000.SH", "name": "浦发银行"}], "test", target
            )
            upsert_daily_bars(
                "600000.SH",
                [
                    {
                        "trade_date": "20260831",
                        "open": 20,
                        "high": 20,
                        "low": 20,
                        "close": 20,
                        "vol": 100,
                    },
                    {
                        "trade_date": "20260901",
                        "open": 9.5,
                        "high": 10.2,
                        "low": 9.4,
                        "close": 10,
                        "pre_close": 9.5,
                        "change": 0.5,
                        "pct_chg": 5.2632,
                        "vol": 120,
                    },
                ],
                "tushare",
                target,
            )

            item = list_stock_catalog(target)[0]

            self.assertEqual(item["price"], 10)
            self.assertEqual(item["change"], 0.5)
            self.assertEqual(item["change_pct"], 5.2632)
            self.assertEqual(item["price_kind"], "daily")


if __name__ == "__main__":
    unittest.main()
