"""
RSI (Relative Strength Index) 超买超卖策略

原理：
- RSI < 30: 超卖，可能反弹 → BUY
- RSI > 70: 超买，可能回调 → SELL

参数：
- period: RSI 计算周期（默认 14）
- oversold: 超卖线（默认 30）
- overbought: 超买线（默认 70）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class RSI(Strategy):
    name = "rsi"

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
    ):
        if period < 2:
            raise ValueError("period must be >= 2")
        if oversold >= overbought:
            raise ValueError("oversold must be < overbought")
        
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        
        # 存储价格变化
        self._prices: Deque[float] = deque(maxlen=period + 1)
        self._last_signal: Optional[Side] = None

    def _calculate_rsi(self) -> Optional[float]:
        """计算 RSI 值"""
        if len(self._prices) < self.period + 1:
            return None
        
        # 计算涨跌幅
        gains = []
        losses = []
        
        for i in range(1, len(self._prices)):
            change = self._prices[i] - self._prices[i - 1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains) / len(gains)
        avg_loss = sum(losses) / len(losses)
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi

    def on_bar(self, bar: Bar) -> Signal:
        self._prices.append(bar.close)
        
        rsi = self._calculate_rsi()
        
        if rsi is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        # RSI < 超卖线 → 买入信号
        if rsi < self.oversold and self._last_signal != Side.BUY:
            self._last_signal = Side.BUY
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=1.0 - (rsi / self.oversold)  # 越超卖强度越高
            )
        
        # RSI > 超买线 → 卖出信号
        if rsi > self.overbought and self._last_signal != Side.SELL:
            self._last_signal = Side.SELL
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=(rsi - self.overbought) / (100 - self.overbought)  # 越超买强度越高
            )
        
        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._prices.clear()
        self._last_signal = None

    def get_current_rsi(self) -> Optional[float]:
        """获取当前 RSI 值（用于调试/显示）"""
        return self._calculate_rsi()
