import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ripple_tradePilot.storage import database_path, init_database


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
            self.assertTrue({"users", "user_sessions", "strategies"}.issubset(tables))
            self.assertEqual(version, 3)

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
            self.assertTrue({"username", "password_hash", "role", "created_at"}.issubset(user_columns))
            self.assertTrue({"user_id", "token_hash", "expires_at"}.issubset(session_columns))
            self.assertTrue({"user_id", "visibility", "parameters_json"}.issubset(strategy_columns))
            self.assertEqual(migrated_role, "admin")


if __name__ == "__main__":
    unittest.main()
