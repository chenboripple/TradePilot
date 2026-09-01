import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pandas as pd

from ripple_tradePilot.data.stock_service import (
    InvalidStockSymbolError,
    StockDataService,
)
from ripple_tradePilot.storage.database import (
    load_daily_bars,
    stock_catalog_name,
    upsert_stock_catalog,
)


class FakeStockDataService(StockDataService):
    def _fetch_tushare(self, symbol, start_date, end_date):
        self.requested_range = (start_date, end_date)
        return pd.DataFrame(
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
        )


class FakeTushareLoader:
    catalog_requests = 0

    def __init__(self, token, rate_limit_delay=1.5):
        self.token = token

    def get_daily_bars(self, symbol, start_date, end_date):
        return pd.DataFrame(
            [
                {
                    "trade_date": "20260901",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 100,
                }
            ]
        )

    def get_stock_list(self):
        type(self).catalog_requests += 1
        return pd.DataFrame(
            [
                {
                    "ts_code": "600418.SH",
                    "name": "江淮汽车",
                    "market": "主板",
                    "list_status": "L",
                    "list_date": "20010930",
                }
            ]
        )


class StockDataServiceTest(unittest.TestCase):
    def test_normalizes_stock_exchange_suffix(self):
        self.assertEqual(StockDataService.normalize_symbol("600000"), "600000.SH")
        self.assertEqual(StockDataService.normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(StockDataService.normalize_symbol("830001"), "830001.BJ")
        self.assertEqual(StockDataService.normalize_symbol("920855"), "920855.BJ")
        with self.assertRaises(InvalidStockSymbolError):
            StockDataService.normalize_symbol("abc")

    def test_refresh_stores_daily_bars_in_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text(
                "symbols:\n  - code: 600000.SH\n    name: 测试银行\n",
                encoding="utf-8",
            )
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

    def test_catalog_refresh_persists_latest_names_and_basic_info(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("tushare:\n  token: test-token\nsymbols: []\n", encoding="utf-8")
            service = StockDataService(config_path=config, database=root / "market.db")
            FakeTushareLoader.catalog_requests = 0

            with patch(
                "ripple_tradePilot.data.stock_service.TushareDataLoader",
                FakeTushareLoader,
            ):
                result = service.refresh_catalog()

            self.assertEqual(result["count"], 1)
            self.assertEqual(stock_catalog_name("600418.SH", root / "market.db"), "江淮汽车")
            self.assertEqual(FakeTushareLoader.catalog_requests, 1)

    def test_catalog_refresh_falls_back_to_full_eastmoney_list(self):
        class LimitedTushareLoader(FakeTushareLoader):
            def get_stock_list(self):
                raise RuntimeError("stock_basic rate limited")

        class FakeResponse:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "data": {
                        "diff": [
                            {"f12": "600418", "f14": "江淮汽车"},
                            {"f12": "920855", "f14": "浙江大农"},
                        ]
                    }
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("tushare:\n  token: test-token\n", encoding="utf-8")
            service = StockDataService(config_path=config, database=root / "market.db")
            with (
                patch(
                    "ripple_tradePilot.data.stock_service.TushareDataLoader",
                    LimitedTushareLoader,
                ),
                patch("ripple_tradePilot.data.stock_service.httpx.get", return_value=FakeResponse()),
            ):
                result = service.refresh_catalog()

            self.assertEqual(result, {"count": 2, "source": "eastmoney"})
            self.assertEqual(stock_catalog_name("600418.SH", root / "market.db"), "江淮汽车")
            self.assertEqual(stock_catalog_name("920855.BJ", root / "market.db"), "浙江大农")

    def test_eastmoney_catalog_retries_an_alternate_endpoint_after_disconnect(self):
        class FakeResponse:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"data": {"diff": [{"f12": "600418", "f14": "江淮汽车"}]}}

        with patch(
            "ripple_tradePilot.data.stock_service.httpx.get",
            side_effect=[httpx.RemoteProtocolError("disconnected"), FakeResponse()],
        ) as get:
            records = StockDataService._eastmoney_catalog_records()

        self.assertEqual(records[0]["name"], "江淮汽车")
        self.assertEqual(get.call_count, 2)

    def test_sina_catalog_reads_all_exchange_names(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data

            @staticmethod
            def raise_for_status():
                return None

            def json(self):
                return self.data

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def get(self, url, params):
                if "StockCount" in url:
                    return FakeResponse("3")
                return FakeResponse(
                    [
                        {"symbol": "sh600418", "code": "600418", "name": "江淮汽车"},
                        {"symbol": "sz300750", "code": "300750", "name": "宁德时代"},
                        {"symbol": "bj920855", "code": "920855", "name": "浙江大农"},
                    ]
                )

        with patch("ripple_tradePilot.data.stock_service.httpx.Client", return_value=FakeClient()):
            records = StockDataService._sina_catalog_records()

        self.assertEqual(
            [(item["symbol"], item["name"]) for item in records],
            [
                ("600418.SH", "江淮汽车"),
                ("300750.SZ", "宁德时代"),
                ("920855.BJ", "浙江大农"),
            ],
        )

    def test_catalog_refresh_falls_back_to_sina_when_eastmoney_is_unavailable(self):
        class LimitedTushareLoader(FakeTushareLoader):
            def get_stock_list(self):
                raise RuntimeError("stock_basic rate limited")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text("tushare:\n  token: test-token\n", encoding="utf-8")
            service = StockDataService(config_path=config, database=root / "market.db")
            with (
                patch(
                    "ripple_tradePilot.data.stock_service.TushareDataLoader",
                    LimitedTushareLoader,
                ),
                patch.object(
                    StockDataService,
                    "_eastmoney_catalog_records",
                    side_effect=RuntimeError("unavailable"),
                ),
                patch.object(
                    StockDataService,
                    "_sina_catalog_records",
                    return_value=[{"symbol": "600418.SH", "name": "江淮汽车"}],
                ),
            ):
                result = service.refresh_catalog()

        self.assertEqual(result, {"count": 1, "source": "sina"})

    def test_single_stock_refresh_uses_catalog_name_without_refreshing_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.yaml"
            config.write_text(
                "tushare:\n  token: test-token\nsymbols: []\n",
                encoding="utf-8",
            )
            database = root / "market.db"
            upsert_stock_catalog(
                [{"symbol": "600000.SH", "name": "ST测试"}],
                "test",
                database,
            )
            FakeTushareLoader.catalog_requests = 0
            with patch(
                "ripple_tradePilot.data.stock_service.TushareDataLoader",
                FakeTushareLoader,
            ):
                result = StockDataService(
                    config_path=config, database=database
                ).refresh("600000.SH")

        self.assertEqual(result["name"], "ST测试")
        self.assertEqual(FakeTushareLoader.catalog_requests, 0)
