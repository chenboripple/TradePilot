import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from ripple_tradePilot.data.stock_service import (
    InvalidStockSymbolError,
    StockDataService,
)
from ripple_tradePilot.storage.database import load_daily_bars


class FakeStockDataService(StockDataService):
    def _fetch_tushare(self, symbol, start_date, end_date):
        self.requested_range = (start_date, end_date)
        return (
            pd.DataFrame(
                [
                    {
                        "trade_date": "20260831",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "vol": 100,
                    },
                    {
                        "trade_date": "20260901",
                        "open": 10.5,
                        "high": 12,
                        "low": 10,
                        "close": 11.8,
                        "vol": 120,
                    },
                ]
            ),
            "测试银行",
        )


class StockDataServiceTest(unittest.TestCase):
    def test_normalizes_stock_exchange_suffix(self):
        self.assertEqual(StockDataService.normalize_symbol("600000"), "600000.SH")
        self.assertEqual(StockDataService.normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(StockDataService.normalize_symbol("830001"), "830001.BJ")
        with self.assertRaises(InvalidStockSymbolError):
            StockDataService.normalize_symbol("abc")

    def test_refresh_stores_daily_bars_in_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("symbols: []\n", encoding="utf-8")
            database = root / "market.db"
            service = FakeStockDataService(config_path=config, database=database)

            result = service.refresh("600000", initial_days=365)
            rows = load_daily_bars("600000.SH", database)

            requested_start = datetime.strptime(service.requested_range[0], "%Y%m%d")
            requested_end = datetime.strptime(service.requested_range[1], "%Y%m%d")
            self.assertGreaterEqual((requested_end - requested_start).days, 364)
            self.assertEqual(result["name"], "测试银行")
            self.assertEqual(result["total_rows"], 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["close"], 11.8)
