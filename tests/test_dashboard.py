import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

from ripple_tradePilot.api.dashboard import DashboardService


class DashboardServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump(
                {
                    "symbols": [
                        {
                            "code": "000001.SZ",
                            "name": "测试股票",
                            "asset_class": "stock",
                            "strategy_profile": "股票策略",
                        }
                    ],
                    "futures": [
                        {
                            "code": "IF2609.CFFEX",
                            "name": "测试期货",
                            "strategy_profile": "期货策略",
                        }
                    ],
                    "strategy_profiles": {"股票策略": self._profile()},
                    "futures_strategy_profiles": {"期货策略": self._profile()},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self._write_bars("000001.SZ", 10.0)
        self._write_bars("IF2609.CFFEX", 3800.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _profile():
        return {
            "kind": "combo_vote",
            "ma_fast": 5,
            "ma_slow": 20,
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "bb_period": 20,
            "bb_std": 2,
        }

    def _write_bars(self, symbol, starting_price):
        path = self.data_dir / f"{symbol}.csv"
        first_day = date.today() - timedelta(days=59)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["trade_date", "open", "high", "low", "close", "vol"])
            writer.writeheader()
            for index in range(60):
                close = starting_price + index * 0.2
                writer.writerow(
                    {
                        "trade_date": (first_day + timedelta(days=index)).strftime("%Y%m%d"),
                        "open": close - 0.1,
                        "high": close + 0.3,
                        "low": close - 0.3,
                        "close": close,
                        "vol": 1000 + index,
                    }
                )

    def _service(self):
        return DashboardService(
            data_dir=self.data_dir,
            config_path=self.config_path,
            backtest_db=self.root / "missing.db",
        )

    def test_dashboard_separates_stocks_and_futures(self):
        dashboard = self._service().dashboard()

        self.assertEqual(dashboard["summary"]["by_asset"]["stock"]["configured"], 1)
        self.assertEqual(dashboard["summary"]["by_asset"]["future"]["configured"], 1)
        self.assertEqual({item["asset_class"] for item in dashboard["markets"]}, {"stock", "future"})

    def test_future_market_detail_contains_chart_and_strategy(self):
        detail = self._service().market_detail("IF2609.CFFEX", limit=40)

        self.assertEqual(detail["asset_class"], "future")
        self.assertEqual(detail["exchange"], "CFFEX")
        self.assertEqual(len(detail["bars"]), 40)
        self.assertIn(detail["recommendation"], {"BUY", "SELL", "HOLD"})
        self.assertIsNotNone(detail["indicators"]["ma_slow"])


if __name__ == "__main__":
    unittest.main()
