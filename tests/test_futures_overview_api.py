"""GET /api/futures/overview 的离线测试（tmp DB 预灌 v16 四表，零网络）。

P1 验收相关：端点纯读 DB 装配观察池快照（主力映射/量额/近月到期/最新信号），
网络全部收敛在扫描侧（monitor.futures_scan）。未扫描品种返回 main=null，
前端显示「待扫描」而非报错。
"""

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ripple_tradePilot.api.app import app
from ripple_tradePilot.storage.database import (
    record_notification,
    upsert_futures_bars,
    upsert_futures_contracts,
    upsert_futures_quotes,
)

PASSWORD = "strong-pass-123"


def _contract_row(symbol, product, expiry="20261015", approximate=False):
    return {
        "symbol": symbol, "product": product, "exchange": "SHFE",
        "name": f"{product}主力", "multiplier": 10.0, "tick_size": 1.0,
        "night_start": "21:00", "night_end": "23:00",
        "listed_date": "20251016", "expiry_date": expiry,
        "expiry_is_approximate": approximate, "rule_version": "20260919-p0",
    }


class FuturesOverviewApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.database = root / "tradepilot.db"
        self.config = root / "config.yaml"
        self.config.write_text(
            "symbols: []\nfutures: []\nfutures_risk:\n"
            "  capital: 200000\n  risk_budget_pct: 0.02\n",
            encoding="utf-8",
        )
        self.environment = patch.dict(
            os.environ,
            {
                "TRADEPILOT_BACKTEST_DB": str(self.database),
                "TRADEPILOT_CONFIG": str(self.config),
                "TUSHARE_TOKEN": "",
            },
        )
        self.environment.start()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        # 登录态：overview 在鉴权之后（与 /api/watchlist 同档）；TestClient
        # 沿用会话 cookie，与 test_app_backtest.py 同模式
        self.client.post(
            "/api/auth/register",
            json={"username": "alice", "password": PASSWORD},
        )
        login = self.client.post(
            "/api/auth/login", json={"username": "alice", "password": PASSWORD}
        )
        self.assertEqual(login.status_code, 200, login.text)

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.environment.stop()
        self.temp_dir.cleanup()

    def _get(self):
        return self.client.get("/api/futures/overview")

    def test_requires_login(self):
        # setUp 已登录（cookie 在 client 上），此处用干净 client 验证 401
        with TestClient(app) as anonymous:
            response = anonymous.get("/api/futures/overview")
        self.assertEqual(response.status_code, 401)

    def test_empty_db_returns_all_products_with_null_main(self):
        response = self._get()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        # 首期 5 品种全量返回：未扫描 → main=null（前端显示「待扫描」，不报错）
        self.assertEqual(
            [item["product"] for item in data["items"]], ["CU", "HC", "I", "M", "RB"]
        )
        self.assertTrue(all(item["main"] is None for item in data["items"]))
        self.assertEqual(data["risk"], {"capital": 200000, "risk_budget_pct": 0.02})

    def test_assembles_main_quote_bar_and_signal(self):
        upsert_futures_contracts([
            _contract_row("RB2610.SHFE", "RB"),
            _contract_row("RB2701.SHFE", "RB", expiry="20270115"),
        ])
        upsert_futures_quotes([
            {
                "symbol": "RB2610.SHFE", "price": 3040.0,
                "upper_limit": 3259.0, "lower_limit": 2948.0,
                "margin_per_hand": 2172.8, "margin_is_estimate": False,
                "fee_per_lot": 6.2, "is_main": False,
                "price_time": "2026-09-19 02:35:14", "source": "comm_info",
            },
            {
                "symbol": "RB2701.SHFE", "price": 3104.0,
                "upper_limit": 3320.0, "lower_limit": 2948.0,
                "margin_per_hand": 2260.0, "margin_is_estimate": False,
                "fee_per_lot": 6.2, "is_main": True,
                "price_time": "2026-09-19 02:35:14", "source": "comm_info",
            },
        ])
        upsert_futures_bars("1d", [
            {
                "symbol": "RB2701.SHFE", "trade_date": "20260918", "bar_time": "",
                "open": 3071, "high": 3077, "low": 3039, "close": 3040,
                "volume": 73949, "hold": 264200, "settle": 3050, "source": "sina",
            },
        ])
        # 60m 完成序列：30 根震荡 + 突破 bar（与 test_futures_scan 同构）→ LONG
        minute_rows = []
        from datetime import datetime as _dt, timedelta as _td
        for index in range(30):
            total = 9 * 60 + index * 37
            day_offset, minutes = divmod(total, 24 * 60)
            stamp = (
                _dt(2026, 9, 16) + _td(days=day_offset)
            ).strftime("%Y-%m-%d") + f" {minutes // 60:02d}:{minutes % 60:02d}:00"
            minute_rows.append({
                "symbol": "RB2701.SHFE", "trade_date": "20260916", "bar_time": stamp,
                "open": 3060, "high": 3080 + (index % 3), "low": 3050, "close": 3070,
                "volume": 500, "hold": 260000, "settle": None, "source": "sina",
            })
        minute_rows.append({
            "symbol": "RB2701.SHFE", "trade_date": "20260918", "bar_time": "2026-09-18 23:00:00",
            "open": 3070, "high": 3090, "low": 3065, "close": 3150,
            "volume": 900, "hold": 260000, "settle": None, "source": "sina",
        })
        upsert_futures_bars("60m", minute_rows)
        record_notification(
            "RB2701.SHFE|60m|2026-09-18 23:00:00|donchian20/10@20260919-p0|signal",
            "feishu", "futures_signal", payload_json='{"tilt": "LONG"}',
        )

        response = self._get()
        self.assertEqual(response.status_code, 200, response.text)
        items = {item["product"]: item for item in response.json()["items"]}
        rb = items["RB"]
        # is_main=1 的 RB2701 被装配为主力，非主力的 RB2610 不串位
        self.assertEqual(rb["main"]["symbol"], "RB2701.SHFE")
        self.assertEqual(rb["main"]["price"], 3104.0)
        self.assertFalse(rb["main"]["margin_is_estimate"])
        self.assertEqual(rb["main"]["latest_bar"]["trade_date"], "20260918")
        self.assertEqual(rb["main"]["latest_bar"]["hold"], 264200)
        # 倾向 + 风险由端点用与扫描相同的纯函数即时重算（同口径不漂移）
        self.assertIsNotNone(rb["tilt"])
        self.assertEqual(rb["tilt"]["tilt"], "LONG")
        self.assertEqual(rb["tilt"]["as_of"], "2026-09-18 23:00:00")
        self.assertIsNotNone(rb["risk"])
        self.assertTrue(rb["risk"]["executable"], rb["risk"]["reasons"])
        self.assertGreaterEqual(rb["risk"]["lots"], 1)
        self.assertEqual(rb["signal"]["payload"], {"tilt": "LONG"})
        # 品种规格透出（每跳盈亏供前端提示卡展示）
        self.assertEqual(rb["tick_value"], 10.0)
        self.assertEqual(rb["trade_unit"], "10吨/手")
        # 无数据的品种保持 main=null（CU 无合约/行情/倾向）
        self.assertIsNone(items["CU"]["main"])
        self.assertIsNone(items["CU"]["tilt"])
        self.assertIsNone(items["CU"]["risk"])
        self.assertIsNone(items["CU"]["signal"])

    def test_days_to_expiry_computed_from_contract(self):
        expiry = (date.today() + timedelta(days=12)).strftime("%Y%m%d")
        upsert_futures_contracts([
            _contract_row("HC2610.SHFE", "HC", expiry=expiry, approximate=True),
        ])
        upsert_futures_quotes([{
            "symbol": "HC2610.SHFE", "price": 3200.0,
            "upper_limit": None, "lower_limit": None,
            "margin_per_hand": 2240.0, "margin_is_estimate": True,
            "fee_per_lot": 6.6, "is_main": True,
            "price_time": "2026-09-19 02:35:14", "source": "comm_info",
        }])
        response = self._get()
        hc = {item["product"]: item for item in response.json()["items"]}["HC"]
        self.assertEqual(hc["main"]["days_to_expiry"], 12)
        self.assertTrue(hc["main"]["expiry_is_approximate"])  # DCE/近似到期透出标记


if __name__ == "__main__":
    unittest.main()
