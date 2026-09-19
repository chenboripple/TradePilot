"""P1 观察池→通知闭环的编排测试（roadmap P1 验收核心，全离线）。

fake akshare（patch.dict sys.modules）+ tmp DB，跑 scan_once 全链路：
快照 → 日线同步 → 主力判定（volume 口径覆盖源方「备注」）→ 60m 已完成
bar 倾向 → §5 风险测算 → notify_log 持久化去重（重启不重发）→ 换月/到期提醒。

合成帧列名复刻新浪真实返回（与 test_futures_data.py 同源：P0 §2 实测）。
"""

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from ripple_tradePilot.monitor.futures_scan import scan_once
from ripple_tradePilot.storage.database import (
    init_database,
    list_notifications,
    load_futures_bars,
    load_futures_quote,
    load_futures_quotes,
    upsert_futures_quotes,
)

SCAN_NOW = datetime(2026, 9, 19, 10, 0, 0)  # 周六上午：周五夜盘 bar 均已完成
CONFIG = {
    "futures_risk": {"capital": 100000, "risk_budget_pct": 0.1},
    "notifiers": {"feishu": {"enabled": False}},
}


def comm_frame(rows):
    """futures_comm_info 真实中文列（service 只读其中 8 列）。"""
    return pd.DataFrame(
        rows,
        columns=["合约代码", "现价", "涨停板", "跌停板", "保证金-每手", "手续费",
                 "每跳毛利", "备注", "价格更新时间"],
    )


def daily_frame(rows):
    return pd.DataFrame(
        rows, columns=["date", "open", "high", "low", "close", "volume", "hold", "settle"]
    )


def minute_frame(rows):
    return pd.DataFrame(
        rows, columns=["datetime", "open", "high", "low", "close", "volume", "hold"]
    )


def trading_days(count, end=date(2026, 9, 18)):
    """end 往前的 count 个工作日（升序 YYYY-MM-DD）。"""
    days, cursor = [], end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor.strftime("%Y-%m-%d"))
        cursor -= timedelta(days=1)
    return list(reversed(days))


def daily_series(symbol_volume, days, base_price=3100.0):
    """单合约日线序列：收盘缓涨，末日成交量 = symbol_volume（主力判定依据）。"""
    rows = []
    for index, day in enumerate(days):
        close = base_price + index * 2
        rows.append((day, close - 3, close + 4, close - 5, close,
                     symbol_volume if index == len(days) - 1 else 1000,
                     200000, close))
    return rows


def minute_series(bars=30):
    """60m 序列：前段 3000~3080 震荡，最后一根已完成 close 3150 突破前 20 根高点。

    时间戳每根 +37 分钟（保证 (trade_date, bar_time) 唯一键不撞——normalize
    会按唯一键去重保留后到）；另附一根「未收线」bar（标签晚于 now），断言
    被评估层剔除——否则 close 会参与通道。
    """
    rows = []
    for index in range(bars):
        total = 9 * 60 + index * 37  # 从 09:00 起步，天然跨夜盘/跨日
        day_offset, minutes = divmod(total, 24 * 60)
        stamp = (
            datetime(2026, 9, 16) + timedelta(days=day_offset)
        ).strftime("%Y-%m-%d") + f" {minutes // 60:02d}:{minutes % 60:02d}:00"
        high = 3080 + (index % 3)
        rows.append((stamp, 3060, high, 3050, 3070, 500, 260000))
    # 最后一根完成 bar：收盘 3150 > max(前 20 根 high ≤ 3082) → LONG
    rows.append(("2026-09-18 23:00:00", 3070, 3090, 3065, 3150, 900, 260000))
    # 未收线 bar（now=10-19 10:00 之后）——不得参与倾向评估
    rows.append(("2026-09-19 11:00:00", 3150, 3200, 3140, 3180, 100, 260000))
    return rows


def fake_akshare(**functions):
    module = MagicMock(name="akshare")
    for name, func in functions.items():
        setattr(module, name, func)
    return module


class FuturesScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "futures.db"
        init_database(self.db)
        self.days = trading_days(6)

    def _patch_ak(self, daily_by_symbol, minute_by_symbol=None, comm_rows=None):
        daily_by_symbol = daily_by_symbol or {}
        minute_by_symbol = minute_by_symbol or {}

        def _daily(symbol):
            return daily_frame(daily_by_symbol[symbol])

        def _minute(symbol, period="60"):
            self.assertEqual(period, "60")
            return minute_frame(minute_by_symbol[symbol])

        fake = fake_akshare(
            futures_comm_info=lambda: comm_frame(comm_rows or []),
            futures_zh_daily_sina=_daily,
            futures_zh_minute_sina=_minute,
        )
        return patch.dict(sys.modules, {"akshare": fake})

    def test_full_loop_signal_dedup_and_authoritative_main(self):
        """主链路：快照→日线→主力（volume 覆盖源备注）→60m 倾向→风险→去重。"""
        comm_rows = [
            # 源方备注 rb2610 是主力——volume 口径应判 rb2701（覆盖源标记）
            ("rb2610", 3040.0, 3259.0, 2948.0, 2172.8, 6.2, 10.0,
             "主力合约", "2026-09-19 02:35:14"),
            ("rb2701", 3104.0, 3320.0, 2948.0, 2260.0, 6.2, 10.0,
             "", "2026-09-19 02:35:14"),
        ]
        daily = {
            "RB2610": daily_series(40000, self.days),
            "RB2701": daily_series(80000, self.days),  # 末日量更大 → 主力
        }
        minute = {"RB2701": minute_series()}
        with self._patch_ak(daily, minute, comm_rows):
            report = scan_once(config=CONFIG, path=self.db, now=SCAN_NOW,
                               products=["RB"])

        self.assertEqual(report.errors, [])
        rb = report.products[0]
        self.assertEqual(rb.product, "RB")
        self.assertEqual(rb.contracts_synced, 2)
        self.assertEqual(rb.main_symbol, "RB2701.SHFE")  # volume 口径
        self.assertIsNotNone(rb.tilt)
        self.assertEqual(rb.tilt.tilt, "LONG")
        # 未收线 bar 剔除后 as_of 应是 09-18 23:00 那根，而非 09-19 11:00
        self.assertEqual(rb.tilt.as_of, "2026-09-18 23:00:00")
        self.assertIsNotNone(rb.risk)
        self.assertTrue(rb.risk.executable, rb.risk.reasons)
        self.assertGreaterEqual(rb.risk.lots, 1)

        # DB 落库：is_main 覆盖为 volume 口径（源备注被覆盖）
        main_quote = load_futures_quote("RB2701.SHFE", self.db)
        self.assertEqual(main_quote["is_main"], 1)
        self.assertEqual(load_futures_quote("RB2610.SHFE", self.db)["is_main"], 0)
        self.assertEqual(main_quote["price"], 3104.0)

        # 通知：一条做多倾向（payload 含 tilt/close/as_of），无 roll/expiry
        kinds = [item["kind"] for item in report.outbox]
        self.assertEqual(kinds, ["futures_signal"])
        logs = list_notifications(kind="futures_signal", path=self.db)
        self.assertEqual(len(logs), 1)
        self.assertIn("RB2701.SHFE", logs[0]["dedup_key"])
        self.assertIn("LONG", logs[0]["payload_json"])

        # 同数据重扫（模拟重启后）：去重生效，不再进 outbox / 不新增记录
        with self._patch_ak(daily, minute, comm_rows):
            again = scan_once(config=CONFIG, path=self.db, now=SCAN_NOW,
                              products=["RB"])
        self.assertEqual(again.outbox, [])
        self.assertEqual(
            len(list_notifications(kind="futures_signal", path=self.db)), 1
        )
        self.assertEqual(again.products[0].main_symbol, "RB2701.SHFE")

        # 60m 库内含未收线 bar 原样入库（数据层不删数据，评估层过滤）
        all_bars = load_futures_bars("RB2701.SHFE", "60m", path=self.db)
        self.assertTrue(any(row["bar_time"] == "2026-09-19 11:00:00" for row in all_bars))

    def test_roll_alert_and_near_expiry(self):
        """换月提醒（与库内上一轮主力比对）+ 近月到期提醒（近似锚一次性）。"""
        # 预置上一轮主力 = RB2701（本轮 volume 判回 RB2610 → 触发换月提醒）
        upsert_futures_quotes([{
            "symbol": "RB2701.SHFE", "price": 3104.0, "upper_limit": None,
            "lower_limit": None, "margin_per_hand": 2260.0,
            "margin_is_estimate": False, "fee_per_lot": 6.2, "is_main": True,
            "price_time": "2026-09-18 02:35:14", "source": "comm_info",
        }], self.db)

        comm_rows = [
            ("rb2610", 3040.0, 3259.0, 2948.0, 2172.8, 6.2, 10.0,
             "", "2026-09-19 02:35:14"),
            ("rb2701", 3104.0, 3320.0, 2948.0, 2260.0, 6.2, 10.0,
             "", "2026-09-19 02:35:14"),
        ]
        daily = {
            "RB2610": daily_series(90000, self.days),
            "RB2701": daily_series(30000, self.days),
        }
        minute = {"RB2610": minute_series()}
        with self._patch_ak(daily, minute, comm_rows):
            report = scan_once(config=CONFIG, path=self.db, now=SCAN_NOW,
                               products=["RB"])

        rb = report.products[0]
        self.assertEqual(rb.main_symbol, "RB2610.SHFE")
        self.assertTrue(rb.roll_alert)          # RB2701 → RB2610
        self.assertTrue(rb.near_expiry)         # 到期月 2026-10 距 09-19 仅 12 天
        kinds = {item["kind"] for item in report.outbox}
        # 倾向信号同样触发（合成 60m 序列突破）；三类提醒一次到位
        self.assertEqual(kinds, {"futures_roll", "futures_expiry", "futures_signal"})
        roll = next(item for item in report.outbox if item["kind"] == "futures_roll")
        self.assertIn("RB2701.SHFE", roll["message"])
        self.assertIn("RB2610.SHFE", roll["message"])

        # 同数据重扫：换月/到期提醒各只发一次（持久化去重）
        with self._patch_ak(daily, minute, comm_rows):
            again = scan_once(config=CONFIG, path=self.db, now=SCAN_NOW,
                              products=["RB"])
        self.assertEqual(again.outbox, [])
        self.assertFalse(again.products[0].roll_alert)  # is_main 已翻转为 RB2610

    def test_snapshot_failure_is_reported_not_raised(self):
        """comm_info 全挂：报告 errors 如实返回，不抛异常（单点失败不崩监控）。"""
        def _boom():
            raise RuntimeError("network down")

        with patch.dict(sys.modules, {"akshare": fake_akshare(futures_comm_info=_boom)}):
            report = scan_once(config=CONFIG, path=self.db, now=SCAN_NOW,
                               products=["RB"])
        self.assertFalse(report.ok)
        self.assertTrue(any("合约快照" in error for error in report.errors))
        self.assertEqual(report.products, [])

    def test_insufficient_bars_means_no_tilt_no_signal(self):
        """60m 不足（没法算）→ tilt=None：不发信号、不发「观望」，风险 gate 拒绝。"""
        comm_rows = [
            ("rb2610", 3040.0, 3259.0, 2948.0, 2172.8, 6.2, 10.0,
             "", "2026-09-19 02:35:14"),
        ]
        daily = {"RB2610": daily_series(50000, self.days)}
        minute = {"RB2610": minute_series(bars=5)}  # < entry+ATR 所需
        with self._patch_ak(daily, minute, comm_rows):
            report = scan_once(config=CONFIG, path=self.db, now=SCAN_NOW,
                               products=["RB"])
        rb = report.products[0]
        self.assertEqual(rb.main_symbol, "RB2610.SHFE")
        self.assertIsNone(rb.tilt)
        self.assertIsNotNone(rb.risk)
        self.assertFalse(rb.risk.executable)  # ATR 缺失 → 规则 gate 拒绝
        # 无倾向 → 无信号；但到期提醒与倾向无关（RB2610 到期月 2026-10），仍触发
        kinds = {item["kind"] for item in report.outbox}
        self.assertEqual(kinds, {"futures_expiry"})
        self.assertEqual(list_notifications(kind="futures_signal", path=self.db), [])


if __name__ == "__main__":
    unittest.main()
