"""POST /api/backtest 端点的离线测试（tmp DB + mock 行情，不联网）。"""
import importlib
import math
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app
from ripple_tradePilot.data.stock_service import StockDataUnavailableError
from ripple_tradePilot.storage.database import database_path, upsert_daily_bars

api_module = importlib.import_module("ripple_tradePilot.api.app")

SYMBOL = "002022.SZ"
PASSWORD = "strong-pass-123"


def _seed_rows(count=140, start_day=None):
    """正弦震荡的合法日线：日波动小，不会触发涨跌停拦截。"""
    first_day = start_day or (date.today() - timedelta(days=count + 10))
    rows = []
    for index in range(count):
        close = 10.0 + 1.5 * math.sin(index / 8.0)
        rows.append(
            {
                "trade_date": (first_day + timedelta(days=index)).strftime("%Y%m%d"),
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "vol": 1000 + index,
            }
        )
    return rows


class WebBacktestApiTest(unittest.TestCase):
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

    def register(self, username="alice"):
        response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_requires_login(self):
        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma"}
        )
        self.assertEqual(response.status_code, 401)

    def test_backtest_returns_metrics_and_curve(self):
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        response = self.client.post(
            "/api/backtest",
            json={
                "symbol": SYMBOL,
                "strategy": "ma",
                "bars": 100,
                "cash": 100000,
                "execution": "next_open",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]

        self.assertEqual(data["symbol"], SYMBOL)
        self.assertEqual(data["strategy"], "ma")
        self.assertEqual(data["execution"], "next_open")
        self.assertEqual(data["bar_count"], 100)
        for key in ("total_return", "annual_return", "max_drawdown", "sharpe"):
            self.assertIn(key, data["metrics"])
        for key in ("num_trades", "win_rate", "total_fees"):
            self.assertIn(key, data["trades"])
        self.assertIsInstance(data["halted_by_drawdown"], bool)
        self.assertEqual(data["skipped_fills"], 0)
        self.assertEqual(len(data["equity_curve"]), 100)
        self.assertIn("disclaimer", data)
        for fill in data["fills"]:
            self.assertIn(fill["side"], ("BUY", "SELL"))

    def test_insufficient_data_returns_503_without_network(self):
        self.register()
        # 只有 30 根，不足 60 根门槛；refresh 被 mock，保证不联网
        upsert_daily_bars(SYMBOL, _seed_rows(30), "test", database_path())
        with patch.object(
            api_module.StockDataService,
            "refresh",
            side_effect=StockDataUnavailableError("行情源不可用"),
        ):
            response = self.client.post(
                "/api/backtest", json={"symbol": SYMBOL, "strategy": "rsi"}
            )
        self.assertEqual(response.status_code, 503, response.text)

    def test_rejects_unknown_strategy(self):
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "bogus"}
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
