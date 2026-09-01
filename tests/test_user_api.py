import importlib
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app
from ripple_tradePilot.data.stock_service import StockDataService
from ripple_tradePilot.storage.database import database_path, upsert_daily_bars

api_module = importlib.import_module("ripple_tradePilot.api.app")


class ApiStockDataService:
    stock_name = "测试银行"
    normalize_symbol = staticmethod(StockDataService.normalize_symbol)

    def refresh(self, value, initial_days=365):
        symbol = self.normalize_symbol(value)
        first_day = date.today() - timedelta(days=59)
        rows = []
        for index in range(60):
            close = 10 + index * 0.1
            rows.append(
                {
                    "trade_date": (first_day + timedelta(days=index)).strftime("%Y%m%d"),
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "vol": 1000 + index,
                }
            )
        upsert_daily_bars(symbol, rows, "test", database_path())
        return {
            "symbol": symbol,
            "name": self.stock_name,
            "source": "test",
            "fetched_rows": len(rows),
            "total_rows": len(rows),
            "latest_date": date.today().isoformat(),
        }


class UserApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "tradepilot.db"
        self.config = self.root / "config.yaml"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.config.write_text("symbols: []\nfutures: []\n", encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "TRADEPILOT_BACKTEST_DB": str(self.database),
                "TRADEPILOT_CONFIG": str(self.config),
                "TRADEPILOT_DATA_DIR": str(self.data_dir),
            },
        )
        self.environment.start()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.temp_dir.cleanup()

    def register(self, username):
        response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "strong-pass-123"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["user"]

    def create_strategy(self, name, visibility):
        response = self.client.post(
            "/api/strategies",
            json={
                "name": name,
                "asset_class": "stock",
                "symbol": "600309.SH",
                "profile": "组合投票",
                "parameters": {"ma_fast": 5, "ma_slow": 20},
                "visibility": visibility,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["item"]

    def test_guests_only_receive_public_dashboard_sections(self):
        dashboard = self.client.get("/api/dashboard")

        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn("strategies", dashboard.json())
        self.assertNotIn("backtests", dashboard.json())
        self.assertEqual(self.client.get("/api/strategies").status_code, 401)
        self.assertEqual(self.client.get("/api/backtests").status_code, 401)

    def test_public_strategies_are_shared_but_private_strategies_are_not(self):
        alice = self.register("alice")
        self.assertEqual(alice["role"], "admin")
        public_strategy = self.create_strategy("开放策略", "public")
        self.create_strategy("私有策略", "private")
        self.client.post("/api/auth/logout")

        bob = self.register("bob")
        self.assertEqual(bob["role"], "user")
        items = self.client.get("/api/strategies").json()["items"]

        self.assertEqual([item["name"] for item in items], ["开放策略"])
        self.assertEqual(items[0]["owner"], alice["username"])
        self.assertFalse(items[0]["is_owner"])
        forbidden = self.client.patch(
            f"/api/strategies/{public_strategy['id']}/visibility",
            json={"visibility": "private"},
        )
        self.assertEqual(forbidden.status_code, 404)

    def test_backtests_are_scoped_to_current_user(self):
        alice = self.register("alice")
        self.client.post("/api/auth/logout")
        bob = self.register("bob")
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO backtest_results (symbol, name, user_id) VALUES (?, ?, ?)",
                ("600309.SH", "Alice record", alice["id"]),
            )
            connection.execute(
                "INSERT INTO backtest_results (symbol, name, user_id) VALUES (?, ?, ?)",
                ("002022.SZ", "Bob record", bob["id"]),
            )

        items = self.client.get("/api/backtests").json()["items"]

        self.assertEqual([item["name"] for item in items], ["Bob record"])

    def test_email_can_be_used_as_case_insensitive_account_name(self):
        user = self.register("Pilot.User+cn@Example.COM")

        self.assertEqual(user["username"], "pilot.user+cn@example.com")
        self.client.post("/api/auth/logout")
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "PILOT.USER+CN@EXAMPLE.COM",
                "password": "strong-pass-123",
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["user"]["username"], "pilot.user+cn@example.com")

    def test_watchlist_is_user_scoped_and_daily_bars_are_visible(self):
        self.assertEqual(self.client.post("/api/watchlist", json={"symbol": "600000"}).status_code, 401)
        self.register("alice")
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            added = self.client.post("/api/watchlist", json={"symbol": "600000"})
            self.assertEqual(added.status_code, 201, added.text)
            refreshed = self.client.post("/api/watchlist/600000.SH/refresh")
            self.assertEqual(refreshed.status_code, 200, refreshed.text)

        items = self.client.get("/api/watchlist").json()["items"]
        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual([item["symbol"] for item in items], ["600000.SH"])
        self.assertEqual([item["symbol"] for item in dashboard["markets"]], ["600000.SH"])
        self.assertTrue(dashboard["markets"][0]["user_added"])

        self.client.post("/api/auth/logout")
        self.register("bob")
        self.assertEqual(self.client.get("/api/watchlist").json()["items"], [])
        self.assertEqual(self.client.get("/api/dashboard").json()["markets"], [])
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            forbidden = self.client.post("/api/watchlist/600000.SH/refresh")
        self.assertEqual(forbidden.status_code, 404)

    def test_refresh_updates_name_and_archived_stock_can_be_restored_without_refresh(self):
        self.register("alice")
        with patch.object(api_module, "StockDataService", ApiStockDataService):
            added = self.client.post("/api/watchlist", json={"symbol": "600000"})
        self.assertEqual(added.status_code, 201, added.text)

        ApiStockDataService.stock_name = "更新后的银行"
        try:
            with patch.object(api_module, "StockDataService", ApiStockDataService):
                refreshed = self.client.post("/api/watchlist/600000.SH/refresh")
        finally:
            ApiStockDataService.stock_name = "测试银行"
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(
            self.client.get("/api/watchlist").json()["items"][0]["name"],
            "更新后的银行",
        )
        self.assertEqual(
            self.client.get("/api/dashboard").json()["markets"][0]["name"],
            "更新后的银行",
        )

        removed = self.client.delete("/api/watchlist/600000.SH")
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(self.client.get("/api/dashboard").json()["markets"], [])
        archived = self.client.get("/api/stocks").json()["items"][0]
        self.assertEqual(archived["name"], "更新后的银行")
        self.assertFalse(archived["is_watched"])

        with patch.object(api_module, "StockDataService", ApiStockDataService):
            forbidden = self.client.post("/api/watchlist/600000.SH/refresh")
        self.assertEqual(forbidden.status_code, 404, forbidden.text)

        class CachedOnlyStockDataService(ApiStockDataService):
            def refresh(self, value, initial_days=365):
                raise AssertionError("restoring an archived stock must not refresh data")

        with patch.object(api_module, "StockDataService", CachedOnlyStockDataService):
            restored = self.client.post(
                "/api/watchlist", json={"symbol": "600000.SH"}
            )
        self.assertEqual(restored.status_code, 201, restored.text)
        self.assertIsNone(restored.json()["data"])
        self.assertEqual(
            self.client.get("/api/dashboard").json()["markets"][0]["name"],
            "更新后的银行",
        )

    def test_configured_stock_can_be_archived_and_restored(self):
        self.config.write_text(
            "symbols:\n"
            "  - code: 600001.SH\n"
            "    name: 默认银行\n"
            "    asset_class: stock\n"
            "futures: []\n",
            encoding="utf-8",
        )
        ApiStockDataService().refresh("600001")
        self.register("alice")

        before = self.client.get("/api/dashboard").json()["markets"]
        self.assertEqual([item["symbol"] for item in before], ["600001.SH"])
        removed = self.client.delete("/api/watchlist/600001.SH")
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(self.client.get("/api/dashboard").json()["markets"], [])
        catalog = self.client.get("/api/stocks").json()["items"]
        self.assertEqual(len(catalog), 1)
        self.assertTrue(catalog[0]["is_default"])
        self.assertFalse(catalog[0]["is_watched"])

        class CachedOnlyStockDataService(ApiStockDataService):
            def refresh(self, value, initial_days=365):
                raise AssertionError("restoring a configured stock must not refresh data")

        with patch.object(api_module, "StockDataService", CachedOnlyStockDataService):
            restored = self.client.post(
                "/api/watchlist", json={"symbol": "600001.SH"}
            )
        self.assertEqual(restored.status_code, 201, restored.text)
        self.assertEqual(
            [item["symbol"] for item in self.client.get("/api/dashboard").json()["markets"]],
            ["600001.SH"],
        )


if __name__ == "__main__":
    unittest.main()
