"""
Donchian Channel Breakout (唐奇安通道突破) 策略

原理：
- 上轨：N 日最高价
- 下轨：N 日最低价
- 突破上轨 → BUY (趋势跟随)
- 跌破下轨 → SELL (趋势反转)

参数：
- window: 通道窗口（默认 20）
- exit_window: 退出窗口（可选，默认使用 window 的一半）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class DonchianBreakout(Strategy):
    name = "donchian"

    def __init__(
        self,
        window: int = 20,
        exit_window: Optional[int] = None,
    ):
        if window < 5:
            raise ValueError("window must be >= 5")
        
        self.window = window
        self.exit_window = exit_window or max(5, window // 2)
        
        # 存储价格
        self._highs: Deque[float] = deque(maxlen=window)
        self._lows: Deque[float] = deque(maxlen=window)
        self._exit_highs: Deque[float] = deque(maxlen=self.exit_window)
        self._exit_lows: Deque[float] = deque(maxlen=self.exit_window)
        self._last_signal: Optional[Side] = None

    def _calculate_channels(self) -> Optional[Tuple[float, float, float, float]]:
        """
        计算唐奇安通道
        
        Returns:
            (upper, lower, exit_upper, exit_lower) 或 None (数据不足)
        """
        if len(self._highs) < self.window or len(self._lows) < self.window:
            return None
        
        upper = max(self._highs)
        lower = min(self._lows)
        
        exit_upper = max(self._exit_highs) if len(self._exit_highs) >= self.exit_window else None
        exit_lower = min(self._exit_lows) if len(self._exit_lows) >= self.exit_window else None
        
        return (upper, lower, exit_upper, exit_lower)

    def on_bar(self, bar: Bar) -> Signal:
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._exit_highs.append(bar.high)
        self._exit_lows.append(bar.low)
        
        channels = self._calculate_channels()
        
        if channels is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        upper, lower, exit_upper, exit_lower = channels
        
        # 突破上轨 → 买入信号（趋势跟随）
        if bar.close > upper and self._last_signal != Side.BUY:
            self._last_signal = Side.BUY
            # 计算强度：突破幅度
            strength = min(1.0, (bar.close - upper) / (upper - lower + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=strength
            )
        
        # 跌破下轨 → 卖出信号
        if bar.close < lower and self._last_signal != Side.SELL:
            self._last_signal = Side.SELL
            strength = min(1.0, (lower - bar.close) / (upper - lower + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=strength
            )
        
        # 多头退出：跌破退出下轨
        if self._last_signal == Side.BUY and exit_lower and bar.close < exit_lower:
            self._last_signal = Side.SELL
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=0.5
            )
        
        # 空头退出：突破退出上轨
        if self._last_signal == Side.SELL and exit_upper and bar.close > exit_upper:
            self._last_signal = Side.BUY
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=0.5
            )
        
        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._exit_highs.clear()
        self._exit_lows.clear()
        self._last_signal = None

    def get_current_channels(self) -> Optional[dict]:
        """获取当前通道值"""
        channels = self._calculate_channels()
        if channels is None:
            return None
        
        upper, lower, exit_upper, exit_lower = channels
        return {
            'upper': upper,
            'lower': lower,
            'exit_upper': exit_upper,
            'exit_lower': exit_lower,
            'width': upper - lower,
        }
