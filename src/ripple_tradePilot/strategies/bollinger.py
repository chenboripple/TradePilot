"""
Bollinger Bands (布林带) 策略

原理：
- 中轨：N 日简单移动平均线
- 上轨：中轨 + K 倍标准差
- 下轨：中轨 - K 倍标准差

交易逻辑（回归策略）：
- 价格触及下轨 → BUY (超卖反弹)
- 价格触及上轨 → SELL (超买回调)

参数：
- period: 周期（默认 20）
- std_dev: 标准差倍数（默认 2.0）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class BollingerBands(Strategy):
    name = "bollinger"

    def __init__(
        self,
        period: int = 20,
        std_dev: float = 2.0,
    ):
        if period < 2:
            raise ValueError("period must be >= 2")
        if std_dev <= 0:
            raise ValueError("std_dev must be > 0")
        
        self.period = period
        self.std_dev = std_dev
        
        # 存储收盘价
        self._prices: Deque[float] = deque(maxlen=period)
        self._last_signal: Optional[Side] = None

    def _calculate_bands(self) -> Optional[Tuple[float, float, float]]:
        """
        计算布林带
        
        Returns:
            (upper, middle, lower) 或 None (数据不足)
        """
        if len(self._prices) < self.period:
            return None
        
        prices = list(self._prices)
        
        # 中轨 = SMA
        middle = sum(prices) / len(prices)
        
        # 标准差
        variance = sum((p - middle) ** 2 for p in prices) / len(prices)
        std = variance ** 0.5
        
        # 上下轨
        upper = middle + (self.std_dev * std)
        lower = middle - (self.std_dev * std)
        
        return (upper, middle, lower)

    def on_bar(self, bar: Bar) -> Signal:
        self._prices.append(bar.close)
        
        bands = self._calculate_bands()
        
        if bands is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        upper, middle, lower = bands
        
        # 价格跌破下轨 → 买入信号（超卖反弹）
        if bar.close <= lower and self._last_signal != Side.BUY:
            self._last_signal = Side.BUY
            # 计算强度：跌破越深强度越高
            strength = min(1.0, (lower - bar.close) / (middle - lower + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=strength
            )
        
        # 价格突破上轨 → 卖出信号（超买回调）
        if bar.close >= upper and self._last_signal != Side.SELL:
            self._last_signal = Side.SELL
            # 计算强度：突破越深强度越高
            strength = min(1.0, (bar.close - upper) / (upper - middle + 0.001))
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=strength
            )
        
        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._prices.clear()
        self._last_signal = None

    def get_current_bands(self) -> Optional[dict]:
        """获取当前布林带值（用于调试/显示）"""
        bands = self._calculate_bands()
        if bands is None:
            return None
        
        upper, middle, lower = bands
        return {
            'upper': upper,
            'middle': middle,
            'lower': lower,
            'width': upper - lower,  # 带宽（波动率指标）
        }
