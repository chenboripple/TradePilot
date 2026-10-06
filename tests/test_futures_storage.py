"""期货 P1/P2 存储层测试（v16 四表：合约/K线/快照/通知去重；v17 三表：订单/成交/账户快照）。

沿房屋契约：tmp DB 全链路 upsert→load 往返、幂等、v15 旧库升级、去重键防重发
（roadmap P1「保证重启后不重复通知」的持久化基础）。
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ripple_tradePilot.storage import init_database
from ripple_tradePilot.storage.database import (
    DATABASE_SCHEMA_VERSION,
    delete_notification,
    insert_futures_orders,
    insert_futures_trades,
    latest_futures_bar,
    list_futures_bar_symbols,
    list_notifications,
    load_futures_account_daily,
    load_futures_bars,
    load_futures_contracts,
    load_futures_quote,
    load_futures_quotes,
    load_futures_orders,
    load_futures_trades,
    record_notification,
    upsert_futures_account_daily,
    upsert_futures_bars,
    upsert_futures_contracts,
    upsert_futures_quotes,
)


class FuturesStorageSchemaTest(unittest.TestCase):
    def test_fresh_db_has_v17_tables(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "f17.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertTrue(
                {"futures_contracts", "futures_bars", "futures_quotes",
                 "notify_log", "futures_orders", "futures_trades",
                 "futures_account_daily"}.issubset(tables)
            )
            self.assertEqual(version, 17)
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_legacy_v15_db_upgrades_to_v17(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "legacy15.db"
            init_database(target)
            with sqlite3.connect(target) as connection:
                for table in ("futures_contracts", "futures_bars",
                              "futures_quotes", "notify_log",
                              "futures_orders", "futures_trades",
                              "futures_account_daily"):
                    connection.execute(f"DROP TABLE {table}")
                connection.execute("PRAGMA user_version=15")
            init_database(target, force=True)  # 强制重跑引导，补建 v16/v17 七表
            with sqlite3.connect(target) as connection:
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertTrue(
                {"futures_contracts", "futures_bars", "futures_quotes",
                 "notify_log", "futures_orders", "futures_trades",
                 "futures_account_daily"}.issubset(tables)
            )
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)


class FuturesContractStoreTest(unittest.TestCase):
    def test_upsert_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "c.db"
            self.assertEqual(
                upsert_futures_contracts(
                    [{
                        "symbol": "RB2610.SHFE", "product": "RB", "exchange": "SHFE",
                        "name": "螺纹钢2610", "multiplier": 10.0, "tick_size": 1.0,
                        "night_start": "21:00", "night_end": "23:00",
                        "listed_date": "20251016", "expiry_date": "20261015",
                        "expiry_is_approximate": False,
                        "rule_version": "20260919-p0",
                    }],
                    target,
                ),
                1,
            )
            # 二次 upsert：expiry 为空串不得清掉已有值（DCE 近似口径保护）
            upsert_futures_contracts(
                [{
                    "symbol": "RB2610.SHFE", "product": "RB", "exchange": "SHFE",
                    "name": "", "multiplier": 10.0, "tick_size": 1.0,
                    "listed_date": "", "expiry_date": "",
                    "expiry_is_approximate": True, "rule_version": "20260920-x",
                }],
                target,
            )
            rows = load_futures_contracts(target)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["name"], "螺纹钢2610")   # 空串保留旧值
            self.assertEqual(row["expiry_date"], "20261015")
            self.assertEqual(row["rule_version"], "20260920-x")
            self.assertEqual(row["expiry_is_approximate"], 1)  # bool → INTEGER


class FuturesBarStoreTest(unittest.TestCase):
    ROWS_1D = [
        {"symbol": "RB2610.SHFE", "trade_date": "20260917", "bar_time": "",
         "open": 3079, "high": 3087, "low": 3064, "close": 3071,
         "volume": 57745, "hold": 290571, "settle": 3075, "source": "sina"},
        {"symbol": "RB2610.SHFE", "trade_date": "20260918", "bar_time": "",
         "open": 3071, "high": 3077, "low": 3039, "close": 3040,
         "volume": 73949, "hold": 264200, "settle": 3050, "source": "sina"},
    ]
    ROWS_60M = [
        {"symbol": "RB2610.SHFE", "trade_date": "20260921", "bar_time": "2026-09-18 22:00:00",
         "open": 3040, "high": 3051, "low": 3040, "close": 3049,
         "volume": 7159, "hold": 262832, "settle": None, "source": "sina"},
        {"symbol": "RB2610.SHFE", "trade_date": "20260921", "bar_time": "2026-09-18 23:00:00",
         "open": 3049, "high": 3055, "low": 3045, "close": 3050,
         "volume": 6400, "hold": 260883, "settle": None, "source": "sina"},
    ]

    def test_upsert_idempotent_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "b.db"
            upsert_futures_bars("1d", self.ROWS_1D, target)
            # 同 key 重写 close → 覆盖不重复；增量重拉的幂等语义
            upsert_futures_bars(
                "1d", [{**self.ROWS_1D[1], "close": 3044}], target
            )
            rows = load_futures_bars("RB2610.SHFE", "1d", path=target)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["close"], 3044)
            self.assertEqual(rows[0]["trade_date"], "20260917")  # 升序

    def test_timeframes_are_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "b2.db"
            upsert_futures_bars("1d", self.ROWS_1D, target)
            upsert_futures_bars("60m", self.ROWS_60M, target)
            # 夜盘 bar 归属交易日 20260921（周五夜盘 → 下一交易日），bar_time 保留原标签
            bars_60m = load_futures_bars("RB2610.SHFE", "60m", path=target)
            self.assertEqual(len(bars_60m), 2)
            self.assertTrue(all(row["trade_date"] == "20260921" for row in bars_60m))
            self.assertEqual(bars_60m[0]["bar_time"], "2026-09-18 22:00:00")
            self.assertEqual(len(load_futures_bars("RB2610.SHFE", "1d", path=target)), 2)

    def test_limit_returns_latest_n_ascending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "b3.db"
            rows = [
                {"symbol": "M2701.DCE", "trade_date": f"2026091{d}", "bar_time": "",
                 "open": 1, "high": 1, "low": 1, "close": d, "volume": 1, "hold": 1,
                 "settle": None, "source": "sina"}
                for d in (0, 1, 2, 3, 4)
            ]
            upsert_futures_bars("1d", rows, target)
            latest = load_futures_bars("M2701.DCE", "1d", limit=2, path=target)
            self.assertEqual([row["trade_date"] for row in latest],
                             ["20260913", "20260914"])

    def test_latest_bar_and_symbols(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "b4.db"
            upsert_futures_bars("1d", self.ROWS_1D, target)
            latest = latest_futures_bar("RB2610.SHFE", "1d", target)
            self.assertIsNotNone(latest)
            self.assertEqual(latest["trade_date"], "20260918")
            self.assertIsNone(latest_futures_bar("RB2701.SHFE", "1d", target))
            self.assertEqual(list_futures_bar_symbols("1d", target), ["RB2610.SHFE"])


class FuturesQuoteStoreTest(unittest.TestCase):
    def test_upsert_and_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "q.db"
            upsert_futures_quotes(
                [{
                    "symbol": "RB2701.SHFE", "price": 3104.0,
                    "upper_limit": 3259.0, "lower_limit": 2948.0,
                    "margin_per_hand": 2172.8, "margin_is_estimate": False,
                    "fee_per_lot": 6.2, "is_main": True,
                    "price_time": "2026-09-19 02:35:14", "source": "comm_info",
                }],
                target,
            )
            row = load_futures_quote("RB2701.SHFE", target)
            self.assertEqual(row["price"], 3104.0)
            self.assertEqual(row["is_main"], 1)
            self.assertEqual(row["margin_is_estimate"], 0)
            self.assertEqual(len(load_futures_quotes(target)), 1)
            self.assertIsNone(load_futures_quote("CU2610.SHFE", target))


class FuturesBacktestAuditStoreTest(unittest.TestCase):
    """v17 审计三表：一次回测运行（run_id）的订单→成交→账户快照可完整重放。"""

    def test_orders_trades_roundtrip_by_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "audit.db"
            self.assertEqual(insert_futures_orders([
                # 一拒一成：拒单（涨跌停）与成交单都要留痕，重放报告才完整
                {"id": 1, "run_id": "run-a", "symbol": "RB2610.SHFE",
                 "direction": "LONG", "open_close": "OPEN", "lots": 2,
                 "price": None, "status": "REJECTED", "filled_lots": 0,
                 "avg_fill_price": None, "trade_date": "20260921",
                 "bar_time": "2026-09-21 10:30:00", "reason": "limit_up"},
                {"id": 2, "run_id": "run-a", "symbol": "RB2610.SHFE",
                 "direction": "LONG", "open_close": "OPEN", "lots": 2,
                 "price": None, "status": "FILLED", "filled_lots": 2,
                 "avg_fill_price": 3102.0, "trade_date": "20260921",
                 "bar_time": "2026-09-21 11:30:00", "reason": ""},
            ], target), 2)
            self.assertEqual(insert_futures_trades([
                {"run_id": "run-a", "order_id": "2", "symbol": "RB2610.SHFE",
                 "direction": "LONG", "open_close": "OPEN", "price": 3102.0,
                 "lots": 2, "fee": 6.2, "trade_date": "20260921",
                 "bar_time": "2026-09-21 11:30:00", "realized_pnl": None,
                 "close_from_yesterday": None, "close_from_today": None},
            ], target), 1)
            orders = load_futures_orders("run-a", target)
            self.assertEqual([o["id"] for o in orders], [1, 2])  # 升序
            self.assertEqual(orders[0]["status"], "REJECTED")
            self.assertEqual(orders[0]["reason"], "limit_up")
            self.assertEqual(orders[1]["avg_fill_price"], 3102.0)
            trades = load_futures_trades("run-a", target)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["fee"], 6.2)
            self.assertIsNone(trades[0]["realized_pnl"])  # 开仓无已实现盈亏
            # run_id 隔离：另一次运行互不可见
            self.assertEqual(load_futures_orders("run-b", target), [])
            self.assertEqual(load_futures_trades("run-b", target), [])

    def test_account_daily_upsert_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "acct.db"
            row = {"run_id": "run-a", "trade_date": "20260921",
                   "balance": 200496.9, "equity": 200496.9,
                   "available": 196401.9, "margin_occupied": 4095.0,
                   "realized_pnl_today": 0.0, "position_pnl_today": 500.0,
                   "fees_today": 3.1, "exposure_value": 31500.0,
                   "settlement_errors": "[]"}
            self.assertEqual(upsert_futures_account_daily([row], target), 1)
            # 同日重写（盘中→结算后口径更新）→ 覆盖不重复
            upsert_futures_account_daily(
                [{**row, "balance": 200496.9, "position_pnl_today": 500.0}],
                target,
            )
            rows = load_futures_account_daily("run-a", target)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["margin_occupied"], 4095.0)
            # 多日快照按交易日升序（权益曲线顺序敏感）
            upsert_futures_account_daily(
                [{**row, "trade_date": "20260922", "balance": 200600.0}], target
            )
            dates = [r["trade_date"] for r in load_futures_account_daily("run-a", target)]
            self.assertEqual(dates, ["20260921", "20260922"])


class NotifyLogTest(unittest.TestCase):
    def test_dedup_key_prevents_resend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "n.db"
            key = "RB2610.SHFE|60m|2026-09-18 23:00:00|donchian20/10@20260919-p0|signal"
            # 首次 → True（首发，允许发送）；重启后同键 → False（防重发）
            self.assertTrue(record_notification(key, "feishu", "futures_signal",
                                                payload_json='{"tilt": "LONG"}',
                                                path=target))
            self.assertFalse(record_notification(key, "feishu", "futures_signal",
                                                 payload_json='{"tilt": "LONG"}',
                                                 path=target))
            rows = list_notifications(channel="feishu", path=target)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "futures_signal")
            # 不同信号时间 = 新键（同一合约的两次不同信号都要通知）
            key2 = "RB2610.SHFE|60m|2026-09-21 15:00:00|donchian20/10@20260919-p0|signal"
            self.assertTrue(record_notification(key2, "feishu", "futures_signal",
                                                path=target))
            self.assertEqual(len(list_notifications(path=target)), 2)
            self.assertEqual(
                len(list_notifications(kind="futures_roll", path=target)), 0
            )

    def test_delete_notification_rearms_key(self):
        """投递失败后的重武装：删掉被压制的关键，同键可再次首发。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "n2.db"
            key = "CU2610.SHFE|expiry|20261001|expiry"
            self.assertTrue(record_notification(key, "feishu", "futures_expiry",
                                                path=target))
            self.assertFalse(record_notification(key, "feishu", "futures_expiry",
                                                 path=target))  # 已被压制
            self.assertTrue(delete_notification(key, path=target))
            self.assertFalse(delete_notification(key, path=target))  # 无此行
            # 重武装后同键回到首发语义
            self.assertTrue(record_notification(key, "feishu", "futures_expiry",
                                                path=target))


if __name__ == "__main__":
    unittest.main()
