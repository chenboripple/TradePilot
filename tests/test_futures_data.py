"""期货行情适配层测试：全离线零网络。

- 归一化/缺口/新鲜度/主力/到期：纯合成 DataFrame，不需要 akshare；
- fetch_*/快照：用 ``patch.dict(sys.modules, {"akshare": fake})`` 注入假模块
  （futures_service 在方法内惰性 import akshare，patch sys.modules 即可拦住，
  与 test_stock_service.py 直接 patch ak 函数是同一手法的惰性导入版）；
- sync：手写 FakeStore（latest_bar/upsert_bars 鸭子接口），断言幂等。

合成帧的列名必须复刻新浪真实返回（P0 §2 实测）：
日线 ``date,open,high,low,close,volume,hold,settle``；
60m ``datetime,open,high,low,close,volume,hold``；
快照 futures_comm_info（九期网）的中文列。
"""

import sys
import unittest
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pandas as pd

from ripple_tradePilot.data.futures_calendar import TradingCalendar
from ripple_tradePilot.data.futures_meta import PRODUCT_SPECS
from ripple_tradePilot.data.futures_service import (
    Freshness,
    FuturesDataError,
    FuturesDataService,
    FuturesDataUnavailableError,
)

# 与 test_futures_calendar.py 相同的固定日历（工作日=交易日，10-01~10-08 假期）
HOLIDAYS = frozenset(date(2026, 10, day) for day in range(1, 9))


def build_calendar() -> TradingCalendar:
    days = []
    cursor = date(2026, 9, 14)
    while cursor <= date(2026, 10, 16):
        if cursor.weekday() < 5 and cursor not in HOLIDAYS:
            days.append(cursor)
        cursor += timedelta(days=1)
    return TradingCalendar(frozenset(days))


def daily_frame(rows) -> pd.DataFrame:
    """新浪 futures_zh_daily_sina 真实列名（P0 §2）。"""
    return pd.DataFrame(
        rows,
        columns=["date", "open", "high", "low", "close", "volume", "hold", "settle"],
    )


def minute_frame(rows) -> pd.DataFrame:
    """新浪 futures_zh_minute_sina 真实列名（标签=bar 结束时刻）。"""
    return pd.DataFrame(
        rows, columns=["datetime", "open", "high", "low", "close", "volume", "hold"]
    )


def fake_akshare(**functions) -> MagicMock:
    """假 akshare 模块：只挂测试关心的函数，其余调用会炸（防漏网）。"""
    module = MagicMock(name="akshare")
    for name, func in functions.items():
        setattr(module, name, func)
    return module


class FakeStore:
    """store 鸭子协议的内存实现：按 (symbol, trade_date, bar_time) 幂等覆盖，
    upsert_bars 返回**新写入**行数——这就是 sync 幂等断言的依据。"""

    def __init__(self):
        self.bars: Dict[tuple, Dict[str, Any]] = {}

    def latest_bar(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        rows = [
            dict(row, timeframe=timeframe)
            for (sym, _td, _bt), row in self.bars.items()
            if sym == symbol
        ]
        if not rows:
            return None
        return max(rows, key=lambda r: (r["trade_date"], r["bar_time"]))

    def upsert_bars(self, timeframe: str, rows: List[Dict[str, Any]]) -> int:
        written = 0
        for row in rows:
            key = (row["symbol"], row["trade_date"], row["bar_time"])
            if key not in self.bars:
                written += 1
            self.bars[key] = dict(row)
        return written


class NormalizeDailyTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()

    def test_column_names_types_and_ascending_order(self):
        frame = daily_frame(
            [
                ("2026-09-16", 3100.0, 3120.0, 3090.0, 3110.0, 1000, 9000, 3105.0),
                ("2026-09-14", 3080.0, 3090.0, 3070.0, 3085.0, 1100, 8900, 3082.0),
                ("2026-09-15", 3085.0, 3095.0, 3075.0, 3090.0, 1200, 8950, 3088.0),
            ]
        )
        rows = self.service.normalize_daily(frame, "RB2610.SHFE")
        self.assertEqual(
            [row["trade_date"] for row in rows], ["20260914", "20260915", "20260916"]
        )
        row = rows[0]
        self.assertEqual(
            set(row),
            {"symbol", "trade_date", "open", "high", "low", "close", "volume",
             "hold", "settle", "bar_time", "source"},
        )
        self.assertEqual(row["symbol"], "RB2610.SHFE")
        self.assertEqual(row["bar_time"], "")
        self.assertEqual(row["source"], "sina")
        for key in ("open", "high", "low", "close", "volume", "hold", "settle"):
            self.assertIsInstance(row[key], float, key)
        self.assertEqual(row["settle"], 3082.0)

    def test_lowercase_symbol_input_normalized(self):
        rows = self.service.normalize_daily(
            daily_frame([("2026-09-14", 3080.0, 3090.0, 3070.0, 3085.0, 1, 2, 3082.0)]),
            "rb2610",
        )
        self.assertEqual(rows[0]["symbol"], "RB2610.SHFE")

    def test_same_date_dedupe_keeps_last(self):
        # 新浪偶发补发/修正行：同日重复保留后到
        frame = daily_frame(
            [
                ("2026-09-15", 3085.0, 3095.0, 3075.0, 3090.0, 100, 8000, 3088.0),
                ("2026-09-15", 3086.0, 3096.0, 3076.0, 3092.0, 200, 8100, 3090.0),
            ]
        )
        rows = self.service.normalize_daily(frame, "RB2610.SHFE")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 3092.0)
        self.assertEqual(rows[0]["volume"], 200.0)

    def test_dirty_rows_dropped(self):
        frame = daily_frame(
            [
                ("2026-09-14", 3080.0, 3090.0, 3070.0, 3085.0, 1, 2, 3082.0),
                ("2026-09-15", float("nan"), 3095.0, 3075.0, 3090.0, 1, 2, 3088.0),
                ("2026-09-16", 0.0, 3095.0, 3075.0, 3090.0, 1, 2, 3088.0),  # 非正价
                ("2026-09-17", 3090.0, 3070.0, 3095.0, 3080.0, 1, 2, 3085.0),  # 高低倒置
            ]
        )
        rows = self.service.normalize_daily(frame, "RB2610.SHFE")
        self.assertEqual([row["trade_date"] for row in rows], ["20260914"])

    def test_missing_core_column_raises(self):
        bad = pd.DataFrame({"date": ["2026-09-14"], "open": [1.0]})
        with self.assertRaisesRegex(FuturesDataError, "缺少必需列"):
            self.service.normalize_daily(bad, "RB2610.SHFE")

    def test_empty_frame_returns_empty_list(self):
        self.assertEqual(
            self.service.normalize_daily(daily_frame([]), "RB2610.SHFE"), []
        )


class Normalize60mTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()
        self.calendar = build_calendar()

    def test_night_bars_belong_to_next_trading_day(self):
        # 周五(09-18)夜盘 → 周一(09-21)；周五日盘 → 周五
        frame = minute_frame(
            [
                ("2026-09-18 15:00:00", 3080.0, 3090.0, 3070.0, 3085.0, 100, 8900),
                ("2026-09-18 22:00:00", 3090.0, 3100.0, 3085.0, 3095.0, 110, 8950),
                ("2026-09-18 23:00:00", 3095.0, 3105.0, 3090.0, 3100.0, 120, 8960),
                ("2026-09-21 09:00:00", 3100.0, 3110.0, 3095.0, 3105.0, 130, 8970),
            ]
        )
        rows = self.service.normalize_60m(frame, "RB2610.SHFE", self.calendar)
        self.assertEqual(
            [row["trade_date"] for row in rows],
            ["20260918", "20260921", "20260921", "20260921"],
        )
        # bar_time 原样保留（含错位聚合标签），settle 恒 None
        self.assertEqual(rows[1]["bar_time"], "2026-09-18 22:00:00")
        self.assertIsNone(rows[1]["settle"])
        self.assertEqual(rows[1]["source"], "sina")

    def test_cu_cross_midnight_double_labels_same_trade_date(self):
        # P0 §4.2 钉死案例的适配层版本：同一夜盘的自然日标签与交易日标签
        # 都归到 2026-09-21（规则见 futures_calendar.trade_date_of）
        frame = minute_frame(
            [
                ("2026-09-18 22:00:00", 70000.0, 70100.0, 69900.0, 70050.0, 10, 100),
                ("2026-09-18 23:00:00", 70050.0, 70150.0, 70000.0, 70100.0, 11, 101),
                ("2026-09-19 00:00:00", 70100.0, 70200.0, 70050.0, 70150.0, 12, 102),
                ("2026-09-19 01:00:00", 70150.0, 70250.0, 70100.0, 70200.0, 13, 103),
                ("2026-09-21 00:00:00", 70200.0, 70300.0, 70150.0, 70250.0, 14, 104),
            ]
        )
        rows = self.service.normalize_60m(frame, "CU2610.SHFE", self.calendar)
        self.assertEqual([row["trade_date"] for row in rows], ["20260921"] * 5)
        # 去重键是 (trade_date, bar_time)：两种标签的 00:00 bar 键不同，都保留
        self.assertEqual(len({(r["trade_date"], r["bar_time"]) for r in rows}), 5)

    def test_dedupe_by_trade_date_and_bar_time(self):
        frame = minute_frame(
            [
                ("2026-09-16 10:00:00", 3090.0, 3100.0, 3085.0, 3095.0, 100, 8000),
                ("2026-09-16 10:00:00", 3091.0, 3101.0, 3086.0, 3096.0, 110, 8010),
            ]
        )
        rows = self.service.normalize_60m(frame, "RB2610.SHFE", self.calendar)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 3096.0)  # 保留后到

    def test_misaligned_bar_label_kept_verbatim(self):
        # 10:15–10:30 小节休息造成的 11:15/14:15 错位聚合标签：不修正（P0 §4.1）
        frame = minute_frame(
            [("2026-09-16 11:15:00", 3090.0, 3100.0, 3085.0, 3095.0, 100, 8000)]
        )
        rows = self.service.normalize_60m(frame, "RB2610.SHFE", self.calendar)
        self.assertEqual(rows[0]["bar_time"], "2026-09-16 11:15:00")
        self.assertEqual(rows[0]["trade_date"], "20260916")

    def test_empty_frame_returns_empty_list(self):
        self.assertEqual(
            self.service.normalize_60m(minute_frame([]), "RB2610.SHFE", self.calendar),
            [],
        )


class DetectGapsTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()
        self.calendar = build_calendar()

    def test_three_days_missing_one(self):
        gaps = self.service.detect_gaps(
            ["20260914", "20260915", "20260917"], self.calendar
        )
        self.assertEqual(gaps, ["20260916"])

    def test_no_gap_when_complete(self):
        gaps = self.service.detect_gaps(
            ["20260914", "20260915", "20260916", "20260917", "20260918"],
            self.calendar,
        )
        self.assertEqual(gaps, [])

    def test_holiday_spanning_is_not_a_gap(self):
        # 假期两端的交易日相邻（09-30 → 10-09），中间没有交易日可缺
        gaps = self.service.detect_gaps(["20260929", "20260930", "20261009"], self.calendar)
        self.assertEqual(gaps, [])

    def test_weekend_gap_inside_series(self):
        gaps = self.service.detect_gaps(["20260918", "20260921", "20260923"], self.calendar)
        self.assertEqual(gaps, ["20260922"])

    def test_single_date_has_no_gap(self):
        self.assertEqual(self.service.detect_gaps(["20260914"], self.calendar), [])
        self.assertEqual(self.service.detect_gaps([], self.calendar), [])


class FreshnessDailyTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()
        self.calendar = build_calendar()

    def test_trading_day_mid_session(self):
        # 周三盘中：当日日线未收线，期望仍是上一交易日（周二）
        result = self.service.freshness_daily(
            "20260915", date(2026, 9, 16), self.calendar, now=datetime(2026, 9, 16, 10, 0)
        )
        self.assertTrue(result.fresh)
        self.assertEqual(result.expected, "20260915")
        stale = self.service.freshness_daily(
            "20260914", date(2026, 9, 16), self.calendar, now=datetime(2026, 9, 16, 10, 0)
        )
        self.assertFalse(stale.fresh)

    def test_weekend_expects_last_trading_day(self):
        result = self.service.freshness_daily(
            "20260918", date(2026, 9, 19), self.calendar, now=datetime(2026, 9, 19, 10, 0)
        )
        self.assertTrue(result.fresh)
        self.assertEqual(result.expected, "20260918")

    def test_night_session_open_allows_previous_trading_day(self):
        # 周五 21:30 夜盘已开：夜盘属下周一，当前交易日=09-21，
        # 日线只需覆盖到上一交易日（=周五自己，日盘已 15:00 收线）
        result = self.service.freshness_daily(
            "20260918", date(2026, 9, 18), self.calendar, now=datetime(2026, 9, 18, 21, 30)
        )
        self.assertTrue(result.fresh)
        self.assertEqual(result.expected, "20260918")
        stale = self.service.freshness_daily(
            "20260917", date(2026, 9, 18), self.calendar, now=datetime(2026, 9, 18, 21, 30)
        )
        self.assertFalse(stale.fresh)
        self.assertIn("夜盘", stale.detail)

    def test_early_saturday_after_friday_night_close(self):
        # P0 观测场景：周六凌晨（周五夜盘刚收）数据新至 2026-09-18 → 新鲜
        result = self.service.freshness_daily(
            "20260918", date(2026, 9, 19), self.calendar, now=datetime(2026, 9, 19, 1, 30)
        )
        self.assertTrue(result.fresh)
        self.assertEqual(result.expected, "20260918")

    def test_monday_day_session_still_expects_friday(self):
        # 周一盘中，周一的日线（含周日夜盘+周一日盘）未收线 → 期望仍是周五
        result = self.service.freshness_daily(
            "20260918", date(2026, 9, 21), self.calendar, now=datetime(2026, 9, 21, 10, 0)
        )
        self.assertTrue(result.fresh)
        self.assertEqual(result.expected, "20260918")

    def test_no_data_is_not_fresh(self):
        result = self.service.freshness_daily(
            "", date(2026, 9, 16), self.calendar, now=datetime(2026, 9, 16, 10, 0)
        )
        self.assertFalse(result.fresh)
        self.assertIn("无日线数据", result.detail)


class Freshness60mTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()
        self.calendar = build_calendar()
        self.rb = PRODUCT_SPECS["RB"]
        self.cu = PRODUCT_SPECS["CU"]

    def test_day_session_fresh(self):
        result = self.service.freshness_60m(
            datetime(2026, 9, 16, 10, 0), datetime(2026, 9, 16, 10, 30),
            self.rb, self.calendar,
        )
        self.assertTrue(result.fresh)
        self.assertEqual(result.expected, "2026-09-16 10:00:00")

    def test_day_session_stale_beyond_two_bars(self):
        # 10:30 时最近应有 bar 结束于 10:00，最新数据停在 07:00 → 落后 3 根
        result = self.service.freshness_60m(
            datetime(2026, 9, 16, 7, 0), datetime(2026, 9, 16, 10, 30),
            self.rb, self.calendar,
        )
        self.assertFalse(result.fresh)
        self.assertIn("2 根", result.detail)

    def test_exactly_two_bars_lag_is_tolerated(self):
        # 恰好 2 根（08:00 vs 10:00）不算超 → 新鲜（容差含等号）
        result = self.service.freshness_60m(
            datetime(2026, 9, 16, 8, 0), datetime(2026, 9, 16, 10, 30),
            self.rb, self.calendar,
        )
        self.assertTrue(result.fresh)

    def test_night_session_fresh_and_stale(self):
        # 周五 22:30 夜盘进行中：最近应有 bar 结束于 22:00
        fresh = self.service.freshness_60m(
            datetime(2026, 9, 18, 22, 0), datetime(2026, 9, 18, 22, 30),
            self.rb, self.calendar,
        )
        self.assertTrue(fresh.fresh)
        self.assertEqual(fresh.expected, "2026-09-18 22:00:00")
        stale = self.service.freshness_60m(
            datetime(2026, 9, 17, 23, 0), datetime(2026, 9, 18, 22, 30),
            self.rb, self.calendar,
        )
        self.assertFalse(stale.fresh)

    def test_closed_market_on_weekend_is_fresh(self):
        result = self.service.freshness_60m(
            datetime(2026, 9, 18, 23, 0), datetime(2026, 9, 19, 10, 0),
            self.rb, self.calendar,
        )
        self.assertTrue(result.fresh)
        self.assertIn("休市", result.detail)
        self.assertEqual(result.expected, "")

    def test_between_day_close_and_night_open_is_fresh(self):
        # 16:30：日盘已收、夜盘未开 → 不要求更新
        result = self.service.freshness_60m(
            datetime(2026, 9, 16, 15, 0), datetime(2026, 9, 16, 16, 30),
            self.rb, self.calendar,
        )
        self.assertTrue(result.fresh)
        self.assertIn("休市", result.detail)

    def test_cu_cross_midnight_window_open(self):
        # cu 周二夜盘跨零点：周三 00:30 仍在 [周二21:00, 周三01:00) 时段内
        fresh = self.service.freshness_60m(
            datetime(2026, 9, 16, 0, 0), datetime(2026, 9, 16, 0, 30),
            self.cu, self.calendar,
        )
        self.assertTrue(fresh.fresh)
        self.assertEqual(fresh.expected, "2026-09-16 00:00:00")
        stale = self.service.freshness_60m(
            datetime(2026, 9, 15, 21, 0), datetime(2026, 9, 16, 0, 30),
            self.cu, self.calendar,
        )
        self.assertFalse(stale.fresh)

    def test_rb_small_hours_outside_window_is_closed(self):
        # rb 夜盘 23:00 收、无跨零点：周三 00:30 处于休市 → 新鲜
        result = self.service.freshness_60m(
            datetime(2026, 9, 15, 23, 0), datetime(2026, 9, 16, 0, 30),
            self.rb, self.calendar,
        )
        self.assertTrue(result.fresh)
        self.assertIn("休市", result.detail)

    def test_no_data_during_session_is_not_fresh(self):
        result = self.service.freshness_60m(
            None, datetime(2026, 9, 16, 10, 30), self.rb, self.calendar,
        )
        self.assertFalse(result.fresh)


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()
        self.calendar = build_calendar()

    def test_fetch_daily_normalizes_and_uppercases_symbol(self):
        ak = fake_akshare(
            futures_zh_daily_sina=MagicMock(
                return_value=daily_frame(
                    [("2026-09-16", 3100.0, 3120.0, 3090.0, 3110.0, 1000, 9000, 3105.0)]
                )
            )
        )
        with patch.dict(sys.modules, {"akshare": ak}):
            rows = self.service.fetch_daily("rb2610")
        ak.futures_zh_daily_sina.assert_called_once_with(symbol="RB2610")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "RB2610.SHFE")
        self.assertEqual(rows[0]["trade_date"], "20260916")

    def test_fetch_daily_empty_raises_with_symbol(self):
        ak = fake_akshare(futures_zh_daily_sina=MagicMock(return_value=daily_frame([])))
        with patch.dict(sys.modules, {"akshare": ak}):
            with self.assertRaisesRegex(FuturesDataUnavailableError, "RB2610"):
                self.service.fetch_daily("RB2610")

    def test_fetch_daily_upstream_failure_raises(self):
        ak = fake_akshare(
            futures_zh_daily_sina=MagicMock(side_effect=RuntimeError("sina down"))
        )
        with patch.dict(sys.modules, {"akshare": ak}):
            with self.assertRaisesRegex(FuturesDataUnavailableError, "RB2610"):
                self.service.fetch_daily("RB2610")

    def test_fetch_daily_invalid_symbol_never_reaches_akshare(self):
        ak = fake_akshare()
        with patch.dict(sys.modules, {"akshare": ak}):
            with self.assertRaises(ValueError):
                self.service.fetch_daily("TA701")  # 郑商所不在首期范围
        ak.futures_zh_daily_sina.assert_not_called()

    def test_fetch_60m_normalizes_with_calendar(self):
        ak = fake_akshare(
            futures_zh_minute_sina=MagicMock(
                return_value=minute_frame(
                    [("2026-09-18 22:00:00", 3090.0, 3100.0, 3085.0, 3095.0, 110, 8950)]
                )
            )
        )
        with patch.dict(sys.modules, {"akshare": ak}):
            rows = self.service.fetch_60m("CU2610", self.calendar)
        ak.futures_zh_minute_sina.assert_called_once_with(symbol="CU2610", period="60")
        self.assertEqual(rows[0]["trade_date"], "20260921")  # 周五夜盘 → 周一

    def test_fetch_60m_empty_raises(self):
        ak = fake_akshare(
            futures_zh_minute_sina=MagicMock(return_value=minute_frame([]))
        )
        with patch.dict(sys.modules, {"akshare": ak}):
            with self.assertRaisesRegex(FuturesDataUnavailableError, "RB2610"):
                self.service.fetch_60m("RB2610", self.calendar)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.calendar = build_calendar()
        self.daily = daily_frame(
            [
                ("2026-09-14", 3080.0, 3090.0, 3070.0, 3085.0, 1100, 8900, 3082.0),
                ("2026-09-15", 3085.0, 3095.0, 3075.0, 3090.0, 1200, 8950, 3088.0),
                ("2026-09-16", 3100.0, 3120.0, 3090.0, 3110.0, 1000, 9000, 3105.0),
            ]
        )

    def test_sync_requires_store(self):
        service = FuturesDataService()  # store=None → 仅归一化
        with self.assertRaisesRegex(FuturesDataError, "store"):
            service.sync_contract("RB2610", "1d", self.calendar, now=datetime(2026, 9, 16, 16, 30))

    def test_sync_rejects_unknown_timeframe(self):
        service = FuturesDataService(store=FakeStore())
        ak = fake_akshare()
        with patch.dict(sys.modules, {"akshare": ak}):
            with self.assertRaises(ValueError):
                service.sync_contract("RB2610", "5m", self.calendar)

    def test_sync_daily_is_idempotent(self):
        ak = fake_akshare(futures_zh_daily_sina=MagicMock(return_value=self.daily))
        store = FakeStore()
        service = FuturesDataService(store=store)
        now = datetime(2026, 9, 16, 16, 30)
        with patch.dict(sys.modules, {"akshare": ak}):
            first = service.sync_contract("rb2610", "1d", self.calendar, now=now)
            second = service.sync_contract("RB2610.SHFE", "1d", self.calendar, now=now)

        self.assertEqual(first.symbol, "RB2610.SHFE")
        self.assertEqual(first.timeframe, "1d")
        self.assertEqual(first.rows_written, 3)
        self.assertEqual(first.new_bars, 3)
        self.assertEqual(first.gaps, [])
        self.assertTrue(first.freshness.fresh)
        self.assertEqual(first.freshness.expected, "20260915")  # 盘后仍期望上一交易日
        # 幂等：第二次全量覆盖但不产生新行/新写入，库内仍是 3 行
        self.assertEqual(second.rows_written, 0)
        self.assertEqual(second.new_bars, 0)
        self.assertEqual(len(store.bars), 3)
        latest = store.latest_bar("RB2610.SHFE", "1d")
        self.assertEqual(latest["trade_date"], "20260916")

    def test_sync_daily_reports_gaps(self):
        gappy = daily_frame(
            [
                row
                for row in [
                    ("2026-09-14", 3080.0, 3090.0, 3070.0, 3085.0, 1100, 8900, 3082.0),
                    ("2026-09-15", 3085.0, 3095.0, 3075.0, 3090.0, 1200, 8950, 3088.0),
                    ("2026-09-16", 3100.0, 3120.0, 3090.0, 3110.0, 1000, 9000, 3105.0),
                ]
                if row[0] != "2026-09-15"  # 缺 09-15
            ]
        )
        ak = fake_akshare(futures_zh_daily_sina=MagicMock(return_value=gappy))
        service = FuturesDataService(store=FakeStore())
        with patch.dict(sys.modules, {"akshare": ak}):
            report = service.sync_contract(
                "RB2610", "1d", self.calendar, now=datetime(2026, 9, 16, 16, 30)
            )
        self.assertEqual(report.gaps, ["20260915"])

    def test_sync_60m_is_idempotent(self):
        ak = fake_akshare(
            futures_zh_minute_sina=MagicMock(
                return_value=minute_frame(
                    [
                        ("2026-09-18 22:00:00", 3090.0, 3100.0, 3085.0, 3095.0, 110, 8950),
                        ("2026-09-18 23:00:00", 3095.0, 3105.0, 3090.0, 3100.0, 120, 8960),
                        ("2026-09-21 09:00:00", 3100.0, 3110.0, 3095.0, 3105.0, 130, 8970),
                        ("2026-09-21 10:00:00", 3105.0, 3115.0, 3100.0, 3110.0, 140, 8980),
                    ]
                )
            )
        )
        store = FakeStore()
        service = FuturesDataService(store=store)
        now = datetime(2026, 9, 21, 10, 30)
        with patch.dict(sys.modules, {"akshare": ak}):
            first = service.sync_contract("RB2610", "60m", self.calendar, now=now)
            second = service.sync_contract("RB2610", "60m", self.calendar, now=now)

        self.assertEqual(first.timeframe, "60m")
        self.assertEqual(first.rows_written, 4)
        self.assertEqual(first.new_bars, 4)
        self.assertTrue(first.freshness.fresh)  # 最新 bar 10:00 距应有 10:00 不超 2 根
        self.assertEqual(first.freshness.expected, "2026-09-21 10:00:00")
        self.assertEqual(second.rows_written, 0)
        self.assertEqual(second.new_bars, 0)
        # 库内键含 bar_time；周五夜盘两根已归到 20260921
        keys = sorted(store.bars)
        self.assertEqual(
            [(k[1], k[2]) for k in keys],
            [
                ("20260921", "2026-09-18 22:00:00"),
                ("20260921", "2026-09-18 23:00:00"),
                ("20260921", "2026-09-21 09:00:00"),
                ("20260921", "2026-09-21 10:00:00"),
            ],
        )


class QuoteSnapshotTest(unittest.TestCase):
    """futures_comm_info（九期网）真实中文列名（akshare 1.18.40 源码核对）。"""

    @staticmethod
    def comm_frame(rows) -> pd.DataFrame:
        return pd.DataFrame(
            rows,
            columns=[
                "交易所名称", "合约名称", "合约代码", "现价", "涨停板", "跌停板",
                "保证金-买开", "保证金-卖开", "保证金-每手", "手续费", "每跳毛利",
                "每跳净利", "备注", "手续费更新时间", "价格更新时间",
            ],
        )

    def test_snapshot_filters_to_first_phase_products(self):
        frame = self.comm_frame(
            [
                # 首期品种（小写合约代码 + 主力备注）
                ("上海期货交易所", "螺纹钢", "rb2701", 3104.0, 3259.0, 2949.0,
                 2230.0, 2230.0, 2172.8, 6.2, 10.0, 3.8, "主力合约",
                 "2026-09-18", "2026-09-18 23:00:15"),
                ("上海期货交易所", "螺纹钢", "rb2605", 3080.0, 3234.0, 2926.0,
                 2218.0, 2218.0, 2160.0, 6.2, 10.0, 3.8, "",
                 "2026-09-18", "2026-09-18 23:00:15"),
                ("上海期货交易所", "沪铜", "cu2610", 109530.0, 116700.0, 102360.0,
                 60250.0, 60250.0, 60241.5, 164.2, 50.0, -114.2, "",
                 "2026-09-18", "2026-09-18 23:00:15"),
                ("大连商品交易所", "铁矿石", "i2701", 714.0, 750.0, 678.0,
                 5712.0, 5712.0, 5712.0, 28.6, 50.0, 21.4, "",
                 "2026-09-18", "2026-09-18 23:00:15"),
                # 非首期品种：郑商所（空响应源）与上期能源 → 整行过滤
                ("郑州商品交易所", "PTA", "ta609", 4800.0, 5040.0, 4560.0,
                 12000.0, 12000.0, 12000.0, 6.0, 10.0, 4.0, "",
                 "2026-09-18", "2026-09-18 23:00:15"),
                ("上海国际能源交易中心", "原油", "sc2612", 520.0, 546.0, 494.0,
                 52000.0, 52000.0, 52000.0, 40.0, 100.0, 60.0, "",
                 "2026-09-18", "2026-09-18 23:00:15"),
            ]
        )
        ak = fake_akshare(futures_comm_info=MagicMock(return_value=frame))
        with patch.dict(sys.modules, {"akshare": ak}):
            rows = FuturesDataService().refresh_quote_snapshot()

        self.assertEqual(
            {row["symbol"] for row in rows},
            {"RB2701.SHFE", "RB2605.SHFE", "CU2610.SHFE", "I2701.DCE"},
        )
        by_symbol = {row["symbol"]: row for row in rows}
        rb = by_symbol["RB2701.SHFE"]
        self.assertTrue(rb["is_main"])
        self.assertEqual(rb["price"], 3104.0)
        self.assertEqual(rb["upper_limit"], 3259.0)
        self.assertEqual(rb["lower_limit"], 2949.0)
        self.assertEqual(rb["margin_per_hand"], 2172.8)
        self.assertEqual(rb["fee_per_lot"], 6.2)
        self.assertEqual(rb["price_time"], "2026-09-18 23:00:15")
        self.assertEqual(rb["source"], "comm_info")
        self.assertFalse(by_symbol["RB2605.SHFE"]["is_main"])  # 同品种非主力保留
        self.assertFalse(by_symbol["CU2610.SHFE"]["is_main"])

    def test_snapshot_skips_rows_with_missing_price(self):
        # 单行缺现价（未挂牌）→ 跳过该行不炸整体
        frame = self.comm_frame(
            [
                ("上海期货交易所", "螺纹钢", "rb2701", 3104.0, 3259.0, 2949.0,
                 2230.0, 2230.0, 2172.8, 6.2, 10.0, 3.8, "主力合约",
                 "2026-09-18", "2026-09-18 23:00:15"),
                ("上海期货交易所", "螺纹钢", "rb2605", float("nan"), 3234.0, 2926.0,
                 2218.0, 2218.0, 2160.0, 6.2, 10.0, 3.8, "",
                 "2026-09-18", "2026-09-18 23:00:15"),
            ]
        )
        ak = fake_akshare(futures_comm_info=MagicMock(return_value=frame))
        with patch.dict(sys.modules, {"akshare": ak}):
            rows = FuturesDataService().refresh_quote_snapshot()
        self.assertEqual([row["symbol"] for row in rows], ["RB2701.SHFE"])

    def test_snapshot_upstream_failure_raises(self):
        ak = fake_akshare(futures_comm_info=MagicMock(side_effect=RuntimeError("9qihuo down")))
        with patch.dict(sys.modules, {"akshare": ak}):
            with self.assertRaises(FuturesDataUnavailableError):
                FuturesDataService().refresh_quote_snapshot()

    def test_snapshot_missing_symbol_column_raises(self):
        ak = fake_akshare(
            futures_comm_info=MagicMock(return_value=pd.DataFrame({"现价": [1.0]}))
        )
        with patch.dict(sys.modules, {"akshare": ak}):
            with self.assertRaisesRegex(FuturesDataUnavailableError, "合约代码"):
                FuturesDataService().refresh_quote_snapshot()


class ResolveMainTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()

    @staticmethod
    def rows(*items):
        # resolve_main 只消费 trade_date/volume/hold（真实入参是 normalize_daily 的行）
        return [
            {"trade_date": trade_date, "volume": volume, "hold": hold}
            for trade_date, volume, hold in items
        ]

    def test_highest_volume_on_latest_common_date_wins(self):
        main = self.service.resolve_main(
            {
                "RB2610.SHFE": self.rows(("20260917", 3000, 90000), ("20260918", 1000, 85000)),
                "RB2701.SHFE": self.rows(("20260917", 1000, 10000), ("20260918", 2500, 20000)),
            }
        )
        self.assertEqual(main, "RB2701.SHFE")

    def test_main_switch_between_days(self):
        # 09-17 主力还是 RB2610（量大），09-18 切到 RB2701 → 判定取最新共同日
        before = self.service.resolve_main(
            {
                "RB2610.SHFE": self.rows(("20260917", 3000, 90000)),
                "RB2701.SHFE": self.rows(("20260917", 1000, 10000)),
            }
        )
        self.assertEqual(before, "RB2610.SHFE")
        after = self.service.resolve_main(
            {
                "RB2610.SHFE": self.rows(("20260917", 3000, 90000), ("20260918", 1000, 85000)),
                "RB2701.SHFE": self.rows(("20260917", 1000, 10000), ("20260918", 2500, 20000)),
            }
        )
        self.assertEqual(after, "RB2701.SHFE")

    def test_hold_breaks_volume_tie(self):
        # volume 相同 → hold 大者（P0 §5：hold 只作并列参考）
        main = self.service.resolve_main(
            {
                "RB2610.SHFE": self.rows(("20260918", 1000, 90000)),
                "RB2701.SHFE": self.rows(("20260918", 1000, 95000)),
            }
        )
        self.assertEqual(main, "RB2701.SHFE")

    def test_farther_expiry_breaks_full_tie(self):
        # volume/hold 全并列 → 到期月份更远者（换月双合约并量时的稳定序）
        main = self.service.resolve_main(
            {
                "RB2611.SHFE": self.rows(("20260918", 1000, 90000)),
                "RB2701.SHFE": self.rows(("20260918", 1000, 90000)),
            }
        )
        self.assertEqual(main, "RB2701.SHFE")

    def test_uses_latest_date_common_to_all_series(self):
        # RB2701 缺 09-18 → 共同最新日是 09-17，按该日 volume 判（无前视）
        main = self.service.resolve_main(
            {
                "RB2610.SHFE": self.rows(("20260917", 1000, 90000), ("20260918", 500, 85000)),
                "RB2701.SHFE": self.rows(("20260917", 2000, 20000)),
            }
        )
        self.assertEqual(main, "RB2701.SHFE")

    def test_no_data_raises(self):
        with self.assertRaisesRegex(FuturesDataError, "至少一个"):
            self.service.resolve_main({})
        with self.assertRaisesRegex(FuturesDataError, "至少一个"):
            self.service.resolve_main({"RB2610.SHFE": []})

    def test_no_common_date_raises(self):
        with self.assertRaisesRegex(FuturesDataError, "共同交易日"):
            self.service.resolve_main(
                {
                    "RB2610.SHFE": self.rows(("20260914", 100, 1)),
                    "RB2701.SHFE": self.rows(("20260915", 200, 2)),
                }
            )


class NearExpiryTest(unittest.TestCase):
    def setUp(self):
        self.service = FuturesDataService()

    def test_within_buffer_before_expiry_month(self):
        # RB2610：到期月首日 2026-10-01，今天 09-05 → 距 26 个日历日 ≤ 30
        self.assertTrue(self.service.near_expiry(2026, 10, date(2026, 9, 5)))

    def test_beyond_buffer_not_near(self):
        self.assertFalse(self.service.near_expiry(2026, 10, date(2026, 8, 25)))  # 37 天

    def test_inside_expiry_month_is_near(self):
        self.assertTrue(self.service.near_expiry(2026, 10, date(2026, 10, 15)))

    def test_buffer_days_configurable(self):
        self.assertFalse(self.service.near_expiry(2026, 10, date(2026, 8, 31)))  # 31>30
        self.assertTrue(
            self.service.near_expiry(2026, 10, date(2026, 8, 31), buffer_days=35)
        )


class FreshnessDataclassTest(unittest.TestCase):
    def test_freshness_is_plain_value_object(self):
        result = Freshness(fresh=True, expected="20260918", detail="ok")
        self.assertEqual((result.fresh, result.expected, result.detail), (True, "20260918", "ok"))


if __name__ == "__main__":
    unittest.main()
