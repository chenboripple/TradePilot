"""期货倾向评估测试（roadmap P1 信号语义行：无持仓 → 只输出倾向，永不输出委托）。

手算基准序列（20 根历史 bar，i = 0..19）：
``high=3100+i, low=3090+i, close=3095+i``。逐根 TR 恒为 10
（h−l=10；与前收的偏离 6/4），故 ATR(14) = 10.0，止损距离 = 10×2 = 20，
通道：此前 20 根最高 3119、最低 3090。
"""

import unittest

from ripple_tradePilot.data.futures_meta import RULE_VERSION
from ripple_tradePilot.indicators import atr_series
from ripple_tradePilot.signals.futures_eval import (
    TILT_LONG,
    TILT_NEUTRAL,
    TILT_SHORT,
    DonchianParams,
    FuturesTilt,
    evaluate_tilt,
)


def prior_bars(n: int = 20) -> list:
    """n 根上升历史 bar，TR 恒为 10（见模块 docstring 手算基准）。"""
    return [
        {
            "bar_time": f"2026-09-18T10:{i:02d}:00",
            "open": 3095.0 + i,
            "high": 3100.0 + i,
            "low": 3090.0 + i,
            "close": 3095.0 + i,
            "volume": 1000.0 + i,  # 额外字段可有可无
        }
        for i in range(n)
    ]


def last_bar(high: float, low: float, close: float, open_: float = None) -> dict:
    if open_ is None:
        open_ = close
    return {
        "bar_time": "2026-09-18T11:00:00",
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "hold": 123456,  # 持仓量字段：存在也不影响
    }


DEFAULTS = DonchianParams()


class TiltSemanticsTest(unittest.TestCase):
    def _evaluate(self, bars, params=DEFAULTS, symbol="RB2610", timeframe="60m"):
        return evaluate_tilt(bars, params, symbol=symbol, timeframe=timeframe)

    def test_breakout_up_is_long_with_numbers(self):
        # 收盘 3120 > 此前 20 根最高 3119 → LONG；basis 必含两个数字
        tilt = self._evaluate(prior_bars() + [last_bar(3122.0, 3112.0, 3120.0)])
        self.assertIsInstance(tilt, FuturesTilt)
        self.assertEqual(tilt.tilt, TILT_LONG)
        self.assertEqual(tilt.channel_high, 3119.0)
        self.assertEqual(tilt.channel_low, 3090.0)
        self.assertEqual(tilt.close, 3120.0)
        self.assertEqual(tilt.atr, 10.0)          # 手算：TR 恒 10 → ATR(14)=10
        self.assertEqual(tilt.stop_distance, 20.0)  # 10 × 2.0
        self.assertEqual(tilt.stop_ref_price, 3100.0)  # 3120 − 20
        self.assertIn("3120", tilt.basis)
        self.assertIn("3119", tilt.basis)
        self.assertIn("20", tilt.basis)

    def test_inside_envelope_is_neutral(self):
        # 收盘 3110 位于 [3090, 3119] 内 → 观望；止损参考为 0 且 basis 说明
        tilt = self._evaluate(prior_bars() + [last_bar(3115.0, 3105.0, 3110.0)])
        self.assertEqual(tilt.tilt, TILT_NEUTRAL)
        self.assertEqual(tilt.stop_ref_price, 0.0)
        self.assertEqual(tilt.stop_distance, 20.0)  # ATR 照常计算
        joined = tilt.basis
        self.assertIn("观望", joined)
        self.assertIn("3090", joined)
        self.assertIn("3119", joined)
        self.assertIn("止损", joined)

    def test_breakdown_is_short_with_numbers(self):
        # 收盘 3088 < 此前 20 根最低 3090 → SHORT
        # ATR = (13×10 + 31) / 14 = 11.5（末根 TR = |3083−3114| = 31）
        tilt = self._evaluate(prior_bars() + [last_bar(3093.0, 3083.0, 3088.0)])
        self.assertEqual(tilt.tilt, TILT_SHORT)
        self.assertEqual(tilt.channel_low, 3090.0)
        self.assertEqual(tilt.atr, 11.5)
        self.assertEqual(tilt.stop_distance, 23.0)
        self.assertEqual(tilt.stop_ref_price, 3111.0)  # 3088 + 23
        self.assertIn("3088", tilt.basis)
        self.assertIn("3090", tilt.basis)

    def test_atr_matches_indicators_series(self):
        # 单一 ATR 口径：末值必须等于 indicators.atr_series 的同位结果
        bars = prior_bars() + [last_bar(3093.0, 3083.0, 3088.0)]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        closes = [b["close"] for b in bars]
        tilt = self._evaluate(bars)
        self.assertEqual(tilt.atr, atr_series(highs, lows, closes, 14)[-1])

    def test_touching_channel_edge_is_neutral(self):
        # 收盘恰好等于通道上沿 3119：突破是严格比较 → NEUTRAL
        tilt = self._evaluate(prior_bars() + [last_bar(3124.0, 3114.0, 3119.0)])
        self.assertEqual(tilt.tilt, TILT_NEUTRAL)

    def test_metadata_echo(self):
        bars = prior_bars() + [last_bar(3122.0, 3112.0, 3120.0)]
        tilt = self._evaluate(bars, symbol="RB2610")
        self.assertEqual(tilt.symbol, "RB2610")
        self.assertEqual(tilt.timeframe, "60m")  # 缺省
        self.assertEqual(tilt.as_of, "2026-09-18T11:00:00")  # bar_time 原样
        self.assertEqual(tilt.strategy_key, "donchian")
        self.assertEqual(
            tilt.strategy_version, f"donchian20/10+atr14x2.0@{RULE_VERSION}"
        )
        explicit = self._evaluate(bars, symbol="I2601", timeframe="1d")
        self.assertEqual(explicit.symbol, "I2601")
        self.assertEqual(explicit.timeframe, "1d")


class TiltInsufficientDataTest(unittest.TestCase):
    """None=没法算（不发通知），NEUTRAL=算出来是观望——两者必须区分。"""

    def test_fewer_than_entry_window_plus_one_returns_none(self):
        for n in (0, 1, 10, 20):
            with self.subTest(n=n):
                self.assertIsNone(evaluate_tilt(prior_bars(n), DEFAULTS, symbol="RB2610"))

    def test_atr_period_not_ready_returns_none(self):
        # entry_window=5 够了，但 10 根 < atr_period 14 → ATR 不可得 → None
        params = DonchianParams(entry_window=5, exit_window=3, atr_period=14)
        bars = prior_bars(9) + [last_bar(3122.0, 3112.0, 3120.0)]
        self.assertIsNone(evaluate_tilt(bars, params, symbol="RB2610"))


class TiltPurityTest(unittest.TestCase):
    """纯函数：同输入同输出（通知去重键依赖此性质）。"""

    def test_same_input_same_output(self):
        bars = prior_bars() + [last_bar(3122.0, 3112.0, 3120.0)]
        first = evaluate_tilt(bars, DEFAULTS, symbol="RB2610")
        second = evaluate_tilt(bars, DEFAULTS, symbol="RB2610")
        self.assertEqual(first, second)

    def test_strategy_version_stable_across_params(self):
        cases = [
            (DEFAULTS, f"donchian20/10+atr14x2.0@{RULE_VERSION}"),
            (
                DonchianParams(entry_window=5, exit_window=3, atr_period=7, atr_stop_multiple=1.5),
                f"donchian5/3+atr7x1.5@{RULE_VERSION}",
            ),
            (
                DonchianParams(entry_window=55, exit_window=20, atr_period=20, atr_stop_multiple=3.0),
                f"donchian55/20+atr20x3.0@{RULE_VERSION}",
            ),
        ]
        # 56 根：最长窗口（entry_window=55 + 1）也够，三个参数都能算出结果
        bars = prior_bars(55) + [last_bar(3122.0, 3112.0, 3120.0)]
        for params, expected in cases:
            with self.subTest(params=params):
                tilt = evaluate_tilt(bars, params, symbol="RB2610")
                self.assertIsNotNone(tilt)
                self.assertEqual(tilt.strategy_version, expected)

    def test_custom_params_short_window_semantics(self):
        # entry_window=5：通道只看此前 5 根（i=2..6 → 高点 3102..3106）。
        # 末根 h=3111/l=3101/c=3108，前收 3101 → TR = max(10,10,0) = 10，
        # 故 ATR(7) = 10，止损距离 = 10×1.5 = 15。
        params = DonchianParams(entry_window=5, exit_window=3, atr_period=7, atr_stop_multiple=1.5)
        bars = prior_bars(7) + [last_bar(3111.0, 3101.0, 3108.0)]
        tilt = evaluate_tilt(bars, params, symbol="RB2610")
        self.assertEqual(tilt.tilt, TILT_LONG)
        self.assertEqual(tilt.channel_high, 3106.0)
        self.assertEqual(tilt.atr, 10.0)
        self.assertEqual(tilt.stop_distance, 15.0)  # 10 × 1.5
        self.assertEqual(tilt.stop_ref_price, 3093.0)  # 3108 − 15


class TiltMalformedInputTest(unittest.TestCase):
    """调用方 bug 明确暴露（ValueError），而不是吞掉返回 None。"""

    def test_invalid_params_raise(self):
        cases = [
            DonchianParams(entry_window=0),
            DonchianParams(exit_window=0),
            DonchianParams(atr_period=0),
            DonchianParams(atr_stop_multiple=0.0),
            DonchianParams(atr_stop_multiple=-1.0),
        ]
        for params in cases:
            with self.subTest(params=params):
                with self.assertRaises(ValueError):
                    evaluate_tilt(prior_bars(), params, symbol="RB2610")

    def test_missing_ohlc_field_raises(self):
        bars = prior_bars() + [last_bar(3122.0, 3112.0, 3120.0)]
        del bars[-1]["close"]
        with self.assertRaises(ValueError):
            evaluate_tilt(bars, DEFAULTS, symbol="RB2610")

    def test_missing_bar_time_on_last_bar_raises(self):
        bars = prior_bars() + [last_bar(3122.0, 3112.0, 3120.0)]
        del bars[-1]["bar_time"]
        with self.assertRaises(ValueError):
            evaluate_tilt(bars, DEFAULTS, symbol="RB2610")


if __name__ == "__main__":
    unittest.main()
