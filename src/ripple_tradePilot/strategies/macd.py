"""
MACD (Moving Average Convergence Divergence) 策略

原理：
- DIF = EMA(fast) - EMA(slow)
- DEA = EMA(DIF, signal)
- MACD 柱 = (DIF - DEA) * 2
- DIF 上穿 DEA → BUY (金叉)
- DIF 下穿 DEA → SELL (死叉)

参数：
- fast: 快线周期（默认 12）
- slow: 慢线周期（默认 26）
- signal: 信号线周期（默认 9）
- zero_cross: 是否使用零轴穿越过滤（默认 False）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class MACD(Strategy):
    name = "macd"

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        zero_cross: bool = False,
    ):
        if fast >= slow:
            raise ValueError("fast must be < slow")
        if fast < 2 or slow < 2 or signal < 2:
            raise ValueError("periods must be >= 2")
        
        self.fast = fast
        self.slow = slow
        self.signal_period = signal
        self.zero_cross = zero_cross
        
        # 存储收盘价
        self._closes: Deque[float] = deque(maxlen=slow + signal + 10)
        self._last_dif: Optional[float] = None
        self._last_dea: Optional[float] = None
        self._last_signal: Optional[Side] = None

    def _ema(self, data: list, period: int) -> float:
        """计算 EMA"""
        if len(data) < period:
            return data[-1] if data else 0
        
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period
        
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema

    def _calculate_macd(self) -> Optional[Tuple[float, float, float]]:
        """
        计算 MACD
        
        Returns:
            (dif, dea, histogram) 或 None
        """
        if len(self._closes) < self.slow + self.signal_period:
            return None
        
        closes = list(self._closes)
        
        # 计算 DIF
        fast_ema = self._ema(closes, self.fast)
        slow_ema = self._ema(closes, self.slow)
        dif = fast_ema - slow_ema
        
        # 计算 DEA 需要历史 DIF 值
        # 简化：使用最近 N 个 DIF 近似
        dif_history = []
        for i in range(self.slow, len(closes) + 1):
            w = closes[:i]
            f_ema = self._ema(w, self.fast)
            s_ema = self._ema(w, self.slow)
            dif_history.append(f_ema - s_ema)
        
        if len(dif_history) < self.signal_period:
            return None
        
        dea = self._ema(dif_history, self.signal_period)
        histogram = (dif - dea) * 2
        
        return (dif, dea, histogram)

    def on_bar(self, bar: Bar) -> Signal:
        self._closes.append(bar.close)
        
        macd = self._calculate_macd()
        
        if macd is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        dif, dea, histogram = macd
        
        # 金叉：DIF 上穿 DEA
        if (self._last_dif is not None and 
            self._last_dif <= self._last_dea and 
            dif > dea):
            
            # 零轴过滤
            if self.zero_cross and dif < 0:
                pass  # 零轴下方金叉，信号较弱，跳过
            else:
                self._last_signal = Side.BUY
                strength = min(1.0, abs(histogram) / 0.1)
                self._last_dif = dif
                self._last_dea = dea
                return Signal(
                    timestamp=bar.timestamp,
                    side=Side.BUY,
                    strength=strength
                )
        
        # 死叉：DIF 下穿 DEA
        elif (self._last_dif is not None and 
              self._last_dif >= self._last_dea and 
              dif < dea):
            
            # 零轴过滤
            if self.zero_cross and dif > 0:
                pass  # 零轴上方死叉，信号较弱，跳过
            else:
                self._last_signal = Side.SELL
                strength = min(1.0, abs(histogram) / 0.1)
                self._last_dif = dif
                self._last_dea = dea
                return Signal(
                    timestamp=bar.timestamp,
                    side=Side.SELL,
                    strength=strength
                )
        
        self._last_dif = dif
        self._last_dea = dea
        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._closes.clear()
        self._last_dif = None
        self._last_dea = None
        self._last_signal = None

    def get_current_macd(self) -> Optional[dict]:
        """获取当前 MACD 值"""
        macd = self._calculate_macd()
        if macd is None:
            return None
        
        dif, dea, histogram = macd
        return {
            'dif': dif,
            'dea': dea,
            'histogram': histogram,
        }
