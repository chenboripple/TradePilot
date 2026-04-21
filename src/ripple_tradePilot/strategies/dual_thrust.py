"""
Dual Thrust (双阈) 策略

原理：
- 基于前 N 日的价格范围（High - Low）
- 上轨 = Open + K1 * Range
- 下轨 = Open - K2 * Range
- 突破上轨 → BUY
- 跌破下轨 → SELL

参数：
- lookback: 回看天数（默认 4）
- k1: 上轨系数（默认 0.5）
- k2: 下轨系数（默认 0.5）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class DualThrust(Strategy):
    name = "dual_thrust"

    def __init__(
        self,
        lookback: int = 4,
        k1: float = 0.5,
        k2: float = 0.5,
    ):
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if k1 <= 0 or k2 <= 0:
            raise ValueError("k1 and k2 must be > 0")
        
        self.lookback = lookback
        self.k1 = k1
        self.k2 = k2
        
        # 存储历史数据
        self._bars: Deque[Bar] = deque(maxlen=lookback + 1)
        self._last_signal: Optional[Side] = None

    def _calculate_range(self) -> Optional[Tuple[float, float, float]]:
        """
        计算价格范围和轨道
        
        Returns:
            (range, upper, lower) 或 None (数据不足)
        """
        if len(self._bars) < self.lookback + 1:
            return None
        
        # 前 N 日的最高价和最低价
        hh = max(b.high for b in list(self._bars)[:-1])  # HH = N 日最高
        ll = min(b.low for b in list(self._bars)[:-1])   # LL = N 日最低
        hc = max(b.close for b in list(self._bars)[:-1]) # HC = N 日收盘最高
        lc = min(b.close for b in list(self._bars)[:-1]) # LC = N 日收盘最低
        
        # Range = max(HH - LC, HC - LL)
        price_range = max(hh - lc, hc - ll)
        
        # 当前开盘价
        current_open = self._bars[-1].open
        
        # 上轨 = Open + K1 * Range
        upper = current_open + self.k1 * price_range
        
        # 下轨 = Open - K2 * Range
        lower = current_open - self.k2 * price_range
        
        return (price_range, upper, lower)

    def on_bar(self, bar: Bar) -> Signal:
        self._bars.append(bar)
        
        rails = self._calculate_range()
        
        if rails is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        price_range, upper, lower = rails
        
        # 突破上轨 → 买入信号
        if bar.close > upper and self._last_signal != Side.BUY:
            self._last_signal = Side.BUY
            strength = min(1.0, (bar.close - upper) / (price_range + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=strength
            )
        
        # 跌破下轨 → 卖出信号
        if bar.close < lower and self._last_signal != Side.SELL:
            self._last_signal = Side.SELL
            strength = min(1.0, (lower - bar.close) / (price_range + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=strength
            )
        
        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._bars.clear()
        self._last_signal = None

    def get_current_rails(self) -> Optional[dict]:
        """获取当前轨道值"""
        rails = self._calculate_range()
        if rails is None:
            return None
        
        price_range, upper, lower = rails
        return {
            'range': price_range,
            'upper': upper,
            'lower': lower,
            'width': upper - lower,
        }
