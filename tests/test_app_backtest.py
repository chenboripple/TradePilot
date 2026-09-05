"""POST /api/backtest 端点的离线测试（tmp DB + mock 行情，不联网）。"""
import importlib
import math
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
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
                # 测试机不配置 tushare token，保证基准对比走优雅降级、不联网
                "TUSHARE_TOKEN": "",
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
        # 未请求 benchmark 时不返回该字段，保持默认回测轻量
        self.assertNotIn("benchmark", data)
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

    def test_backtest_result_recorded_for_ledger_page(self):
        """Web 回测成功后应写入回测记录，/api/backtests 能列出。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma", "bars": 100}
        )
        self.assertEqual(response.status_code, 200, response.text)

        listing = self.client.get("/api/backtests")
        self.assertEqual(listing.status_code, 200, listing.text)
        items = listing.json()["items"]
        self.assertEqual(len(items), 1)
        record = items[0]
        self.assertEqual(record["symbol"], SYMBOL)
        self.assertEqual(record["asset_class"], "stock")
        self.assertEqual(record["start_date"][:2], "20")
        self.assertTrue(record["end_date"])
        self.assertIsInstance(record["total_return"], float)
        self.assertIsInstance(record["win_rate"], float)
        # 扩展字段供前端"重跑"：记录回测入参
        self.assertIsInstance(record["id"], int)
        self.assertEqual(record["strategy_key"], "ma")
        self.assertEqual(record["bar_count"], 100)
        self.assertEqual(record["execution"], "next_open")

    def test_rejects_unknown_strategy(self):
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "bogus"}
        )
        self.assertEqual(response.status_code, 422)

    def test_backtest_benchmark_degrades_without_token(self):
        """benchmark=true 但无 tushare token：200 + available=false，不联网。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        with patch.object(
            api_module,
            "TushareDataLoader",
            side_effect=AssertionError("无 token 时不应构造 TushareDataLoader"),
        ):
            response = self.client.post(
                "/api/backtest",
                json={
                    "symbol": SYMBOL,
                    "strategy": "ma",
                    "bars": 100,
                    "benchmark": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        # 回测主体照常返回
        self.assertEqual(data["symbol"], SYMBOL)
        self.assertEqual(len(data["equity_curve"]), 100)
        # 基准优雅降级：available=false、空曲线、收益为 null
        self.assertEqual(
            data["benchmark"],
            {
                "code": "000300.SH",
                "name": "沪深300",
                "available": False,
                "return": None,
                "curve": [],
            },
        )

    def test_backtest_benchmark_curve_normalized(self):
        """benchmark=true 且指数数据可用：归一化曲线首点 1.0，全程离线（mock）。"""
        self.register()
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())

        index_df = pd.DataFrame(
            {
                "trade_date": [
                    (date(2026, 1, 1) + timedelta(days=i)).strftime("%Y%m%d")
                    for i in range(100)
                ],
                "close": [3000.0 + i * 5 for i in range(100)],
            }
        )

        class FakeLoader:
            def __init__(self, token, rate_limit_delay=1.5):
                self.token = token

            def get_index_bars(self, index_code, start_date, end_date):
                return index_df

        with patch.object(api_module, "load_config", return_value={}), patch.object(
            api_module, "get_tushare_token", return_value="fake-token"
        ), patch.object(api_module, "TushareDataLoader", FakeLoader):
            response = self.client.post(
                "/api/backtest",
                json={
                    "symbol": SYMBOL,
                    "strategy": "ma",
                    "bars": 100,
                    "benchmark": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        benchmark = response.json()["data"]["benchmark"]
        self.assertTrue(benchmark["available"])
        self.assertEqual(benchmark["code"], "000300.SH")
        self.assertEqual(benchmark["name"], "沪深300")
        self.assertEqual(len(benchmark["curve"]), 100)
        self.assertEqual(benchmark["curve"][0], {"date": "2026-01-01", "value": 1.0})
        self.assertAlmostEqual(
            benchmark["curve"][-1]["value"], (3000.0 + 99 * 5) / 3000.0, places=4
        )
        self.assertAlmostEqual(
            benchmark["return"], benchmark["curve"][-1]["value"] - 1, places=6
        )

    def test_delete_backtest_record_flow(self):
        """DELETE /api/backtests/{id}：本人 204，重复删/删他人记录均 404。"""
        self.register("alice")
        upsert_daily_bars(SYMBOL, _seed_rows(), "test", database_path())
        response = self.client.post(
            "/api/backtest", json={"symbol": SYMBOL, "strategy": "ma", "bars": 100}
        )
        self.assertEqual(response.status_code, 200, response.text)
        items = self.client.get("/api/backtests").json()["items"]
        self.assertEqual(len(items), 1)
        alice_record = items[0]

        # 注册第二个用户后，无权删除 alice 的记录
        self.register("bob")
        response = self.client.delete(f"/api/backtests/{alice_record['id']}")
        self.assertEqual(response.status_code, 404, response.text)

        # 切回 alice：首次删除 204，同一 id 再删 404
        login = self.client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        self.assertEqual(login.status_code, 200, login.text)
        response = self.client.delete(f"/api/backtests/{alice_record['id']}")
        self.assertEqual(response.status_code, 204, response.text)
        response = self.client.delete(f"/api/backtests/{alice_record['id']}")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("不存在", response.json()["detail"])
        self.assertEqual(self.client.get("/api/backtests").json()["items"], [])

    def test_delete_backtest_requires_login(self):
        response = self.client.delete("/api/backtests/1")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
