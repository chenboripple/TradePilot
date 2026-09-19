"""期货交易日历测试：全部离线，用固定合成日历钉死 P0 §4 的口径。

日历夹具：2026-09-14（周一）~ 2026-10-16（周五）的工作日为交易日，
10-01~10-08 假设为国庆非交易日（覆盖范围内断流=休市）。星期事实在
fixture 测试里再校验一遍，防止后来者改夹具时踩错。
"""

import unittest
from datetime import date, datetime, timedelta

from ripple_tradePilot.data.futures_calendar import (
    TradingCalendar,
    is_night_session,
    trade_date_of,
)
from ripple_tradePilot.data.futures_meta import PRODUCT_SPECS, ProductSpec

# 国庆假设：10-01（周四）~ 10-08（周四）休市
HOLIDAYS = frozenset(date(2026, 10, day) for day in range(1, 9))
CALENDAR_FIRST = date(2026, 9, 14)
CALENDAR_LAST = date(2026, 10, 16)


def build_calendar() -> TradingCalendar:
    """主力品种日线日期集 → 交易日历（P0 §4.4：假期由断流自然体现）。"""
    days = []
    cursor = CALENDAR_FIRST
    while cursor <= CALENDAR_LAST:
        if cursor.weekday() < 5 and cursor not in HOLIDAYS:
            days.append(cursor)
        cursor += timedelta(days=1)
    return TradingCalendar(frozenset(days))


def no_night_spec() -> ProductSpec:
    """无夜盘品种（首期 5 品种都有夜盘，构造一个反例做分支覆盖）。"""
    return ProductSpec(
        code="XX", exchange="TEST", name="测试", multiplier=1.0, tick_size=1.0,
        trade_unit="1吨/手", night_start="", night_end="", night_end_next_day=False,
        margin_rate=0.1, round_trip_fee=1.0,
    )


class CalendarFixtureTest(unittest.TestCase):
    def test_fixture_weekday_facts(self):
        # 夹具的星期前提（写错这里，后面全部作废）
        self.assertEqual(date(2026, 9, 14).weekday(), 0)  # 周一
        self.assertEqual(date(2026, 9, 18).weekday(), 4)  # 周五
        self.assertEqual(date(2026, 9, 19).weekday(), 5)  # 周六
        self.assertEqual(date(2026, 10, 9).weekday(), 4)  # 假期后首个交易日=周五
        self.assertEqual(date(2026, 10, 16).weekday(), 4)


class TradingCalendarTest(unittest.TestCase):
    def setUp(self):
        self.calendar = build_calendar()

    def test_is_trading_day_within_coverage(self):
        for day in (date(2026, 9, 14), date(2026, 9, 18), date(2026, 9, 21),
                    date(2026, 9, 30), date(2026, 10, 9), date(2026, 10, 16)):
            with self.subTest(day=day):
                self.assertTrue(self.calendar.is_trading_day(day))
        # 覆盖范围内的非集合日：周末与假期都是确定结论，不是近似
        for day in (date(2026, 9, 19), date(2026, 9, 20),
                    date(2026, 10, 1), date(2026, 10, 8)):
            with self.subTest(day=day):
                self.assertFalse(self.calendar.is_trading_day(day))
        self.assertFalse(self.calendar.state()["is_approx"])

    def test_is_trading_day_approx_outside_coverage(self):
        # 超出首末日期范围 → 工作日近似，且 state 显式透出近似
        self.assertTrue(self.calendar.is_trading_day(date(2026, 11, 20)))  # 周五
        self.assertFalse(self.calendar.is_trading_day(date(2026, 11, 21)))  # 周六
        self.assertTrue(self.calendar.is_trading_day(date(2026, 9, 7)))  # 覆盖前的周一
        state = self.calendar.state()
        self.assertTrue(state["is_approx"])
        self.assertEqual(state["covered_range"], (CALENDAR_FIRST, CALENDAR_LAST))

    def test_next_trading_day_skips_weekend_and_holidays(self):
        cases = [
            (date(2026, 9, 14), date(2026, 9, 15)),  # 普通次日
            (date(2026, 9, 18), date(2026, 9, 21)),  # 周五 → 下周一
            (date(2026, 9, 19), date(2026, 9, 21)),  # 周六 → 下周一
            (date(2026, 9, 30), date(2026, 10, 9)),  # 假期前 → 假期后
            (date(2026, 10, 1), date(2026, 10, 9)),  # 假期中 → 假期后
            (date(2026, 10, 8), date(2026, 10, 9)),
        ]
        for start, expected in cases:
            with self.subTest(start=start):
                self.assertEqual(self.calendar.next_trading_day(start), expected)
        self.assertFalse(self.calendar.state()["is_approx"])  # 全程在覆盖范围内

    def test_next_trading_day_approx_beyond_coverage(self):
        # 覆盖范围之后退化为工作日近似（周五 → 下周一），并置位 is_approx
        self.assertEqual(
            self.calendar.next_trading_day(date(2026, 10, 16)), date(2026, 10, 19)
        )
        self.assertTrue(self.calendar.state()["is_approx"])

    def test_state_of_empty_calendar(self):
        calendar = TradingCalendar(frozenset())
        state = calendar.state()
        self.assertIsNone(state["covered_range"])
        self.assertTrue(state["is_approx"])  # 空日历本身就是纯近似
        self.assertTrue(calendar.is_trading_day(date(2026, 9, 14)))  # 周一近似
        self.assertEqual(
            calendar.next_trading_day(date(2026, 9, 19)), date(2026, 9, 21)
        )


class TradeDateOfTest(unittest.TestCase):
    """P0 §4.2 实证规则：不信任跨零点 bar 的标签日期，只按小时归属。"""

    def setUp(self):
        self.calendar = build_calendar()
        self.rb = PRODUCT_SPECS["RB"]
        self.cu = PRODUCT_SPECS["CU"]

    def test_cu_cross_midnight_double_labels(self):
        # 必须钉死的案例：cu2610 周五(2026-09-18)夜盘的跨零点 bar，新浪同时存在
        # 自然日标签（2026-09-19 01:00，周六凌晨）与交易日标签（2026-09-21 00:00，
        # 周一）——两种口径都必须归到交易日 2026-09-21。
        for ts, why in [
            (datetime(2026, 9, 19, 1, 0), "自然日标签（周六凌晨）"),
            (datetime(2026, 9, 21, 0, 0), "交易日标签（周一）"),
            (datetime(2026, 9, 18, 22, 0), "同夜盘 22:00 bar"),
            (datetime(2026, 9, 18, 21, 0), "夜盘开市时刻"),
            (datetime(2026, 9, 19, 0, 0), "自然日标签（周六零点）"),
        ]:
            with self.subTest(ts=ts, why=why):
                self.assertEqual(trade_date_of(ts, self.cu, self.calendar), date(2026, 9, 21))
        self.assertFalse(self.calendar.state()["is_approx"])

    def test_day_session_maps_to_natural_day(self):
        for hour in (4, 9, 11, 14, 20):
            with self.subTest(hour=hour):
                self.assertEqual(
                    trade_date_of(datetime(2026, 9, 16, hour, 0), self.rb, self.calendar),
                    date(2026, 9, 16),
                )

    def test_early_morning_includes_self_when_trading_day(self):
        # hour < 4：从自然日起的下一交易日（含自身）——周一凌晨标签归周一
        self.assertEqual(
            trade_date_of(datetime(2026, 9, 21, 3, 0), self.rb, self.calendar),
            date(2026, 9, 21),
        )
        # 周六/周日凌晨 → 下周一
        self.assertEqual(
            trade_date_of(datetime(2026, 9, 19, 3, 0), self.rb, self.calendar),
            date(2026, 9, 21),
        )
        self.assertEqual(
            trade_date_of(datetime(2026, 9, 20, 2, 30), self.rb, self.calendar),
            date(2026, 9, 21),
        )

    def test_night_after_21_maps_to_next_trading_day(self):
        self.assertEqual(
            trade_date_of(datetime(2026, 9, 16, 21, 0), self.rb, self.calendar),
            date(2026, 9, 17),
        )
        # 假期前夜（09-30 周三 22:00）→ 跳过整个国庆到 10-09
        self.assertEqual(
            trade_date_of(datetime(2026, 9, 30, 22, 0), self.rb, self.calendar),
            date(2026, 10, 9),
        )
        # 假期中的错误标签（数据不该出现，规则仍给出确定行为）
        self.assertEqual(
            trade_date_of(datetime(2026, 10, 1, 22, 0), self.rb, self.calendar),
            date(2026, 10, 9),
        )


class NightSessionTest(unittest.TestCase):
    def test_rb_night_window_half_open(self):
        rb = PRODUCT_SPECS["RB"]  # 21:00–23:00
        for ts, expected in [
            (datetime(2026, 9, 16, 21, 0), True),    # 开市时刻在内
            (datetime(2026, 9, 16, 22, 59), True),
            (datetime(2026, 9, 16, 23, 0), False),   # 收市时刻不在内（左闭右开）
            (datetime(2026, 9, 16, 20, 59), False),
            (datetime(2026, 9, 16, 10, 0), False),   # 日盘
        ]:
            with self.subTest(ts=ts):
                self.assertEqual(is_night_session(ts, rb), expected)

    def test_cu_night_window_crosses_midnight(self):
        cu = PRODUCT_SPECS["CU"]  # 21:00–次日01:00（night_end_next_day）
        for ts, expected in [
            (datetime(2026, 9, 16, 23, 59), True),
            (datetime(2026, 9, 17, 0, 0), True),
            (datetime(2026, 9, 17, 0, 59), True),
            (datetime(2026, 9, 17, 1, 0), False),    # 次日收市时刻
            (datetime(2026, 9, 16, 20, 0), False),
            (datetime(2026, 9, 16, 14, 0), False),
        ]:
            with self.subTest(ts=ts):
                self.assertEqual(is_night_session(ts, cu), expected)

    def test_product_without_night_session_is_always_false(self):
        spec = no_night_spec()
        for hour in (0, 10, 22, 23):
            with self.subTest(hour=hour):
                self.assertFalse(is_night_session(datetime(2026, 9, 16, hour, 0), spec))


if __name__ == "__main__":
    unittest.main()
