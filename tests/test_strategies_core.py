"""
核心策略行为夹具测试：锁定信号语义（边沿触发、阈值、去重），
保证回测/监控/看板三处复用的是同一套因果正确的信号逻辑。
"""

import unittest
from datetime import datetime, timedelta
from typing import List

from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI


def bars_from_closes(closes: List[float]) -> List[Bar]:
    base = datetime(2026, 1, 5)
    return [
        Bar(
            timestamp=base + timedelta(days=i),
            open=c,
            high=c * 1.001,
            low=c * 0.999,
            close=c,
            volume=1000,
        )
        for i, c in enumerate(closes)
    ]


class MovingAverageCrossTest(unittest.TestCase):
    def test_no_signal_before_warmup(self):
        strategy = MovingAverageCross(fast=2, slow=5)
        for bar in bars_from_closes([1, 2, 3, 4]):
            self.assertIsNone(strategy.on_bar(bar).side)

    def test_golden_cross_fires_once(self):
        """边沿触发：金叉只在翻转当根发一次信号，不重复。"""
        closes = [10, 10, 10, 10, 10, 12, 13, 14, 15, 16]
        strategy = MovingAverageCross(fast=2, slow=5)
        signals = [strategy.on_bar(bar) for bar in bars_from_closes(closes)]
        buy_indices = [i for i, s in enumerate(signals) if s.side == Side.BUY]
        self.assertEqual(len(buy_indices), 1)

    def test_reset_clears_state(self):
        strategy = MovingAverageCross(fast=2, slow=5)
        for bar in bars_from_closes([10, 12, 14, 16, 18]):
            strategy.on_bar(bar)
        strategy.reset()
        self.assertIsNone(strategy.on_bar(bars_from_closes([10])[0]).side)


class RSITest(unittest.TestCase):
    def test_strong_decline_triggers_buy(self):
        closes = [50 - i for i in range(20)]  # 持续下跌 → 超卖
        strategy = RSI(period=14, oversold=30, overbought=70)
        sides = [strategy.on_bar(bar).side for bar in bars_from_closes(closes)]
        self.assertIn(Side.BUY, sides)
        self.assertNotIn(Side.SELL, sides)

    def test_strong_rally_triggers_sell(self):
        closes = [10 + i for i in range(20)]  # 持续上涨 → 超买
        strategy = RSI(period=14, oversold=30, overbought=70)
        sides = [strategy.on_bar(bar).side for bar in bars_from_closes(closes)]
        self.assertIn(Side.SELL, sides)
        self.assertNotIn(Side.BUY, sides)

    def test_signal_is_edge_triggered(self):
        """RSI 停留在超卖区时不重复发信号。"""
        closes = [50 - i for i in range(30)]
        strategy = RSI(period=6, oversold=30, overbought=70)
        sides = [strategy.on_bar(bar).side for bar in bars_from_closes(closes)]
        self.assertEqual(sides.count(Side.BUY), 1)

    def test_current_rsi_accessor(self):
        """监控 breakout 画像依赖 get_current_rsi()（旧代码访问 _last_rsi 属性不存在）。"""
        strategy = RSI(period=6)
        for bar in bars_from_closes([10 + i for i in range(10)]):
            strategy.on_bar(bar)
        self.assertIsNotNone(strategy.get_current_rsi())


if __name__ == "__main__":
    unittest.main()
