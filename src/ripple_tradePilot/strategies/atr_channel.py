"""
ATR Channel (平均真实波幅通道) 策略

原理：
- 使用 ATR 衡量波动率
- 中轨 = N 日收盘价均线
- 上轨 = 中轨 + K * ATR
- 下轨 = 中轨 - K * ATR
- 突破上轨 → BUY (趋势跟随)
- 跌破下轨 → SELL

参数：
- period: ATR 计算周期（默认 14）
- channel_period: 通道中轨周期（默认 20）
- multiplier: ATR 倍数（默认 2.0）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class ATRChannel(Strategy):
    name = "atr_channel"

    def __init__(
        self,
        period: int = 14,
        channel_period: int = 20,
        multiplier: float = 2.0,
    ):
        if period < 2:
            raise ValueError("period must be >= 2")
        if channel_period < 2:
            raise ValueError("channel_period must be >= 2")
        if multiplier <= 0:
            raise ValueError("multiplier must be > 0")
        
        self.period = period
        self.channel_period = channel_period
        self.multiplier = multiplier
        
        # 存储数据
        self._bars: Deque[Bar] = deque(maxlen=max(period, channel_period) + 1)
        self._closes: Deque[float] = deque(maxlen=channel_period)
        self._last_atr: Optional[float] = None
        self._last_signal: Optional[Side] = None

    def _calculate_atr(self) -> Optional[float]:
        """计算 ATR"""
        if len(self._bars) < self.period + 1:
            return None
        
        bars = list(self._bars)
        tr_sum = 0.0
        
        for i in range(1, len(bars)):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_sum += tr
        
        return tr_sum / self.period

    def _calculate_channel(self) -> Optional[Tuple[float, float, float]]:
        """
        计算通道
        
        Returns:
            (upper, middle, lower) 或 None
        """
        if len(self._closes) < self.channel_period:
            return None
        
        atr = self._calculate_atr()
        if atr is None:
            return None
        
        self._last_atr = atr  # 保存 ATR 值
        middle = sum(self._closes) / len(self._closes)
        upper = middle + self.multiplier * atr
        lower = middle - self.multiplier * atr
        
        return (upper, middle, lower)

    def on_bar(self, bar: Bar) -> Signal:
        self._bars.append(bar)
        self._closes.append(bar.close)
        
        channel = self._calculate_channel()
        
        if channel is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        upper, middle, lower = channel
        
        # 突破上轨 → 买入
        if bar.close > upper and self._last_signal != Side.BUY:
            self._last_signal = Side.BUY
            strength = min(1.0, (bar.close - upper) / (self.multiplier * self._last_atr + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=strength
            )
        
        # 跌破下轨 → 卖出
        if bar.close < lower and self._last_signal != Side.SELL:
            self._last_signal = Side.SELL
            strength = min(1.0, (lower - bar.close) / (self.multiplier * self._last_atr + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=strength
            )
        
        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._bars.clear()
        self._closes.clear()
        self._last_atr = None
        self._last_signal = None

    def get_current_channel(self) -> Optional[dict]:
        """获取当前通道值"""
        channel = self._calculate_channel()
        if channel is None:
            return None
        
        upper, middle, lower = channel
        return {
            'upper': upper,
            'middle': middle,
            'lower': lower,
            'atr': self._last_atr,
            'width': upper - lower,
        }
