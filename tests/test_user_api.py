import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app


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


if __name__ == "__main__":
    unittest.main()
