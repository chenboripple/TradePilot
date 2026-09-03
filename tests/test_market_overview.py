import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app
from ripple_tradePilot.data import stock_service
from ripple_tradePilot.data.mx_loader import MXDataLoader
from ripple_tradePilot.data.stock_service import (
    StockDataService,
    StockDataUnavailableError,
)
from ripple_tradePilot.storage.database import upsert_stock_quotes


SAMPLE_INDICES = [
    {"name": "上证指数", "code": "sh000001", "price": 3200.12, "change": 15.23, "change_pct": 0.48},
    {"name": "深证成指", "code": "sz399001", "price": 10500.55, "change": -32.10, "change_pct": -0.31},
    {"name": "创业板指", "code": "sz399006", "price": 2150.33, "change": 8.66, "change_pct": 0.40},
]


def _mx_table_payload(columns, values):
    """构造符合 MXDataLoader._extract_price_data 解析结构的妙想响应。"""
    table = {"headName": ["2026-09-03"]}
    name_map = {}
    for index, label in enumerate(columns):
        key = f"col{index}"
        table[key] = [values[index]]
        name_map[key] = label
    return {
        "status": 0,
        "data": {
            "data": {
                "searchDataResultDTO": {
                    "dataTableDTOList": [{"table": table, "nameMap": name_map}]
                }
            }
        },
    }


def _mx_index_payload(price, change, change_pct):
    return _mx_table_payload(
        ["最新价", "涨跌额", "涨跌幅"], [price, change, change_pct]
    )


class MarketOverviewTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "tradepilot.db"
        self.config = self.root / "config.yaml"
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.config.write_text("symbols: []\nfutures: []\n", encoding="utf-8")
        # MX_APIKEY 置空，避免测试机真实环境变量触发妙想网络请求
        self.environment = patch.dict(
            os.environ,
            {
                "TRADEPILOT_BACKTEST_DB": str(self.database),
                "TRADEPILOT_CONFIG": str(self.config),
                "TRADEPILOT_DATA_DIR": str(self.data_dir),
                "MX_APIKEY": "",
            },
        )
        self.environment.start()
        # 指数行情有模块级 60 秒缓存，测试间必须清理
        stock_service._INDEX_QUOTE_CACHE.clear()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        stock_service._INDEX_QUOTE_CACHE.clear()
        self.temp_dir.cleanup()

    def register(self, username="alice"):
        response = self.client.post(
            "/api/auth/register",
            json={"username": username, "password": "strong-pass-123"},
        )
        self.assertEqual(response.status_code, 201, response.text)

    def seed_quotes(self, quote_time, rows=None):
        """写入合成快照：涨/平/跌/涨停/跌停各若干。"""
        if rows is None:
            rows = [
                {"symbol": "600001.SH", "price": 10.5, "change_pct": 2.5, "amount": 1_000_000_000.0},
                {"symbol": "600002.SH", "price": 21.89, "change_pct": 9.9, "amount": 2_000_000_000.0},
                {"symbol": "600003.SH", "price": 5.0, "change_pct": 0.0, "amount": 500_000_000.0},
                {"symbol": "600004.SH", "price": 8.0, "change_pct": None, "amount": None},
                {"symbol": "600005.SH", "price": 15.2, "change_pct": -3.2, "amount": 3_000_000_000.0},
                {"symbol": "600006.SH", "price": 9.13, "change_pct": -9.85, "amount": 400_000_000.0},
            ]
        records = [{**row, "quote_time": quote_time} for row in rows]
        upsert_stock_quotes(records, "test", self.database)

    def mock_market_sources(self, stack, indices=None, indices_source="", mx_breadth=None):
        """mock 掉 refresh_quotes / fetch_index_quotes / fetch_market_breadth_mx。"""
        refresh = stack.enter_context(patch.object(StockDataService, "refresh_quotes"))
        stack.enter_context(
            patch.object(
                StockDataService,
                "fetch_index_quotes",
                return_value={"indices": indices if indices is not None else [], "source": indices_source},
            )
        )
        stack.enter_context(
            patch.object(
                StockDataService, "fetch_market_breadth_mx", return_value=mx_breadth
            )
        )
        return refresh

    def test_overview_requires_login(self):
        response = self.client.get("/api/market/overview")
        self.assertEqual(response.status_code, 401, response.text)

    def test_overview_contract_shape_and_values(self):
        self.register()
        quote_time = datetime.now().isoformat(timespec="seconds")
        self.seed_quotes(quote_time)

        with ExitStack() as stack:
            refresh = self.mock_market_sources(
                stack, indices=SAMPLE_INDICES, indices_source="sina"
            )
            response = self.client.get("/api/market/overview")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(
            set(data),
            {"quote_time", "indices", "breadth", "turnover", "sentiment", "stale", "source"},
        )
        self.assertEqual(data["quote_time"], quote_time)
        self.assertEqual(data["indices"], SAMPLE_INDICES)
        self.assertEqual(
            data["breadth"],
            {"total": 6, "up": 2, "flat": 2, "down": 2, "limit_up": 1, "limit_down": 1},
        )
        self.assertEqual(data["turnover"], 6_900_000_000.0)
        self.assertEqual(data["sentiment"], {"up_ratio": 0.33, "label": "偏弱"})
        self.assertFalse(data["stale"])
        self.assertEqual(data["source"], "sina+snapshot")
        refresh.assert_not_called()

    def test_overview_sentiment_labels(self):
        self.register()
        quote_time = datetime.now().isoformat(timespec="seconds")
        with ExitStack() as stack:
            self.mock_market_sources(stack)
            # 1 涨 1 跌 → up_ratio 0.5 → 均衡
            self.seed_quotes(
                quote_time,
                rows=[
                    {"symbol": "600001.SH", "price": 10.0, "change_pct": 1.0, "amount": 100.0},
                    {"symbol": "600002.SH", "price": 10.0, "change_pct": -1.0, "amount": 100.0},
                ],
            )
            balanced = self.client.get("/api/market/overview").json()["data"]["sentiment"]
            # 2 涨 1 平 → up_ratio 0.67 → 偏强
            self.seed_quotes(
                quote_time,
                rows=[
                    {"symbol": "600001.SH", "price": 10.0, "change_pct": 1.0, "amount": 100.0},
                    {"symbol": "600002.SH", "price": 10.0, "change_pct": 3.0, "amount": 100.0},
                    {"symbol": "600003.SH", "price": 10.0, "change_pct": 0.0, "amount": 100.0},
                ],
            )
            strong = self.client.get("/api/market/overview").json()["data"]["sentiment"]

        self.assertEqual(balanced, {"up_ratio": 0.5, "label": "均衡"})
        self.assertEqual(strong, {"up_ratio": 0.67, "label": "偏强"})

    def test_empty_snapshot_triggers_single_refresh(self):
        self.register()
        fresh_time = datetime.now().isoformat(timespec="seconds")

        def fake_refresh(*args):
            self.seed_quotes(fresh_time)
            return {"count": 6, "source": "test", "quote_time": fresh_time}

        with ExitStack() as stack:
            refresh = self.mock_market_sources(stack)
            refresh.side_effect = fake_refresh
            response = self.client.get("/api/market/overview")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(refresh.call_count, 1)
        data = response.json()["data"]
        self.assertEqual(data["breadth"]["total"], 6)
        self.assertEqual(data["indices"], [])
        self.assertFalse(data["stale"])
        self.assertEqual(data["quote_time"], fresh_time)
        self.assertEqual(data["source"], "snapshot")

    def test_empty_snapshot_and_failed_refresh_returns_503(self):
        self.register()
        with ExitStack() as stack:
            refresh = self.mock_market_sources(stack)
            refresh.side_effect = StockDataUnavailableError("行情源全部不可用")
            response = self.client.get("/api/market/overview")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("暂无全市场行情快照", response.json()["detail"])
        self.assertEqual(refresh.call_count, 1)

    def test_stale_snapshot_served_with_stale_flag_when_refresh_fails(self):
        self.register()
        stale_time = (datetime.now() - timedelta(minutes=10)).isoformat(
            timespec="seconds"
        )
        self.seed_quotes(stale_time)

        with ExitStack() as stack:
            refresh = self.mock_market_sources(stack)
            refresh.side_effect = StockDataUnavailableError("行情源全部不可用")
            response = self.client.get("/api/market/overview")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(refresh.call_count, 1)
        data = response.json()["data"]
        self.assertTrue(data["stale"])
        self.assertEqual(data["quote_time"], stale_time)
        self.assertEqual(data["breadth"]["total"], 6)
        self.assertEqual(data["breadth"]["up"], 2)

    def test_stale_snapshot_is_replaced_after_successful_refresh(self):
        self.register()
        stale_time = (datetime.now() - timedelta(minutes=10)).isoformat(
            timespec="seconds"
        )
        self.seed_quotes(
            stale_time,
            rows=[
                {"symbol": "600001.SH", "price": 9.0, "change_pct": -1.0, "amount": 10.0},
                {"symbol": "600002.SH", "price": 9.0, "change_pct": -2.0, "amount": 10.0},
                {"symbol": "600003.SH", "price": 9.0, "change_pct": -3.0, "amount": 10.0},
            ],
        )
        fresh_time = datetime.now().isoformat(timespec="seconds")
        fresh_rows = [
            {"symbol": "600001.SH", "price": 10.0, "change_pct": 1.0, "amount": 100.0},
            {"symbol": "600002.SH", "price": 10.0, "change_pct": 2.0, "amount": 200.0},
            {"symbol": "600003.SH", "price": 10.0, "change_pct": 0.0, "amount": 300.0},
        ]

        def fake_refresh(*args):
            self.seed_quotes(fresh_time, rows=fresh_rows)
            return {"count": 3, "source": "test", "quote_time": fresh_time}

        with ExitStack() as stack:
            refresh = self.mock_market_sources(stack)
            refresh.side_effect = fake_refresh
            response = self.client.get("/api/market/overview")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(refresh.call_count, 1)
        data = response.json()["data"]
        self.assertFalse(data["stale"])
        self.assertEqual(data["quote_time"], fresh_time)
        self.assertEqual(data["breadth"]["total"], 3)
        self.assertEqual(data["sentiment"]["label"], "偏强")

    def test_mx_breadth_overrides_snapshot_counts(self):
        self.register()
        self.seed_quotes(datetime.now().isoformat(timespec="seconds"))

        with ExitStack() as stack:
            self.mock_market_sources(
                stack,
                indices=SAMPLE_INDICES,
                indices_source="mx",
                mx_breadth={"up": 3000, "down": 2000, "flat": 300},
            )
            response = self.client.get("/api/market/overview")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(
            data["breadth"],
            {"total": 5300, "up": 3000, "flat": 300, "down": 2000, "limit_up": 1, "limit_down": 1},
        )
        self.assertEqual(data["turnover"], 6_900_000_000.0)  # 成交额仍来自本地快照
        self.assertEqual(data["sentiment"], {"up_ratio": 0.57, "label": "均衡"})
        self.assertEqual(data["source"], "mx")

    def test_index_quotes_prefer_mx_when_key_configured(self):
        def fake_query(tool_query):
            if "上证指数" in tool_query:
                return _mx_index_payload(3200.12, 15.23, 0.48)
            if "深证成指" in tool_query:
                return _mx_index_payload(10500.55, -32.10, -0.31)
            if "创业板指" in tool_query:
                return _mx_index_payload(2150.33, 8.66, 0.40)
            raise AssertionError(f"意外的妙想查询：{tool_query}")

        with patch.dict(os.environ, {"MX_APIKEY": "test-mx-key"}), \
                patch.object(MXDataLoader, "_query", side_effect=fake_query) as mx_query, \
                patch(
                    "ripple_tradePilot.data.stock_service.httpx.get",
                    side_effect=AssertionError("命中妙想后不应再请求新浪"),
                ), \
                patch(
                    "ripple_tradePilot.data.stock_service.ak.stock_zh_index_spot_em",
                    side_effect=AssertionError("命中妙想后不应再请求 AkShare"),
                ):
            result = StockDataService().fetch_index_quotes()
            self.assertEqual(result["source"], "mx")
            self.assertEqual(result["indices"], SAMPLE_INDICES)

            # 60 秒内存缓存：第二次调用不再请求妙想
            cached = StockDataService().fetch_index_quotes()
            self.assertEqual(cached, result)
            self.assertEqual(mx_query.call_count, 3)

    def test_index_quotes_fall_back_to_sina_without_mx_key(self):
        sina_text = (
            'var hq_str_s_sh000001="上证指数,3200.12,15.23,0.48,38627278,479852650";\n'
            'var hq_str_s_sz399001="深证成指,10500.55,-32.10,-0.31,450000000,5600000000";\n'
            'var hq_str_s_sz399006="创业板指,2150.33,8.66,0.40,300000000,3900000000";\n'
        )

        class FakeSinaResponse:
            content = sina_text.encode("gbk")

            @staticmethod
            def raise_for_status():
                return None

        with patch(
            "ripple_tradePilot.data.stock_service.httpx.get",
            return_value=FakeSinaResponse(),
        ) as get, patch(
            "ripple_tradePilot.data.stock_service.ak.stock_zh_index_spot_em",
            side_effect=AssertionError("命中新浪后不应再请求 AkShare"),
        ):
            result = StockDataService().fetch_index_quotes()

        self.assertEqual(result["source"], "sina")
        self.assertEqual(result["indices"], SAMPLE_INDICES)
        url = get.call_args[0][0]
        self.assertIn("hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006", url)
        self.assertIn("finance.sina.com.cn", get.call_args[1]["headers"]["Referer"])

    def test_index_quotes_fall_back_to_akshare_when_sina_fails(self):
        frame = pd.DataFrame(
            [
                {"代码": "000001", "名称": "上证指数", "最新价": 3200.12, "涨跌额": 15.23, "涨跌幅": 0.48},
                {"代码": "399001", "名称": "深证成指", "最新价": 10500.55, "涨跌额": -32.10, "涨跌幅": -0.31},
                {"代码": "399006", "名称": "创业板指", "最新价": 2150.33, "涨跌额": 8.66, "涨跌幅": 0.40},
                {"代码": "888888", "名称": "无关指数", "最新价": 1.0, "涨跌额": 0.0, "涨跌幅": 0.0},
            ]
        )
        with patch(
            "ripple_tradePilot.data.stock_service.httpx.get",
            side_effect=RuntimeError("sina disconnected"),
        ), patch(
            "ripple_tradePilot.data.stock_service.ak.stock_zh_index_spot_em",
            return_value=frame,
        ):
            result = StockDataService().fetch_index_quotes()

        self.assertEqual(result["source"], "akshare")
        self.assertEqual(result["indices"], SAMPLE_INDICES)

    def test_index_quotes_empty_when_all_sources_fail(self):
        with patch(
            "ripple_tradePilot.data.stock_service.httpx.get",
            side_effect=RuntimeError("sina disconnected"),
        ), patch(
            "ripple_tradePilot.data.stock_service.ak.stock_zh_index_spot_em",
            side_effect=RuntimeError("akshare disconnected"),
        ):
            result = StockDataService().fetch_index_quotes()

        self.assertEqual(result, {"indices": [], "source": ""})

    def test_market_breadth_mx_parses_counts(self):
        payload = _mx_table_payload(
            ["上涨家数", "下跌家数", "平盘家数"], [3000, 2000, 300]
        )
        with patch.dict(os.environ, {"MX_APIKEY": "test-mx-key"}), patch.object(
            MXDataLoader, "_query", return_value=payload
        ):
            breadth = StockDataService().fetch_market_breadth_mx()

        self.assertEqual(breadth, {"up": 3000, "down": 2000, "flat": 300})

    def test_market_breadth_mx_returns_none_when_unparsable(self):
        with patch.dict(os.environ, {"MX_APIKEY": "test-mx-key"}), patch.object(
            MXDataLoader, "_query", return_value={"status": 1}
        ):
            self.assertIsNone(StockDataService().fetch_market_breadth_mx())

    def test_market_breadth_mx_returns_none_without_key(self):
        self.assertIsNone(StockDataService().fetch_market_breadth_mx())


if __name__ == "__main__":
    unittest.main()
