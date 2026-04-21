"""
Mean Reversion (均值回归) 改进版策略

原理：
- 计算 N 日移动平均和标准差
- Z-Score = (当前价 - 均值) / 标准差
- Z-Score < -entry_std → BUY (超卖)
- Z-Score > exit_std → SELL (超买)
- 可选：使用 ATR 动态调整阈值

参数：
- lookback: 回看周期（默认 20）
- entry_std: 入场标准差倍数（默认 2.0）
- exit_std: 出场标准差倍数（默认 0.5）
- use_atr: 是否使用 ATR 动态调整（默认 False）
- atr_period: ATR 周期（默认 14）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional, Tuple

import numpy as np

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class MeanReversion(Strategy):
    name = "mean_reversion"

    def __init__(
        self,
        lookback: int = 20,
        entry_std: float = 2.0,
        exit_std: float = 0.5,
        use_atr: bool = False,
        atr_period: int = 14,
    ):
        if lookback < 5:
            raise ValueError("lookback must be >= 5")
        if entry_std <= 0 or exit_std <= 0:
            raise ValueError("std thresholds must be > 0")
        
        self.lookback = lookback
        self.entry_std = entry_std
        self.exit_std = exit_std
        self.use_atr = use_atr
        self.atr_period = atr_period
        
        self._closes: Deque[float] = deque(maxlen=lookback)
        self._bars: Deque[Bar] = deque(maxlen=max(lookback, atr_period) + 1)
        self._last_signal: Optional[Side] = None
        self._last_zscore: Optional[float] = None

    def _calculate_zscore(self) -> Optional[Tuple[float, float, float]]:
        """
        计算 Z-Score
        
        Returns:
            (zscore, mean, std) 或 None
        """
        if len(self._closes) < self.lookback:
            return None
        
        closes = list(self._closes)
        mean = float(np.mean(closes))
        std = float(np.std(closes))
        
        if std == 0:
            return None
        
        current_price = closes[-1]
        zscore = (current_price - mean) / std
        
        return (zscore, mean, std)

    def _calculate_atr(self) -> Optional[float]:
        """计算 ATR"""
        if len(self._bars) < self.atr_period + 1:
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
        
        return tr_sum / self.atr_period

    def on_bar(self, bar: Bar) -> Signal:
        self._closes.append(bar.close)
        self._bars.append(bar)
        
        zscore_data = self._calculate_zscore()
        
        if zscore_data is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        zscore, mean, std = zscore_data
        self._last_zscore = zscore
        
        # 动态调整阈值（使用 ATR）
        entry_threshold = self.entry_std
        exit_threshold = self.exit_std
        
        if self.use_atr:
            atr = self._calculate_atr()
            if atr and atr > 0:
                # 根据波动率调整：高波动率时放宽阈值
                vol_adjustment = min(2.0, max(0.5, atr / (std + 0.001)))
                entry_threshold = self.entry_std * vol_adjustment
                exit_threshold = self.exit_std * vol_adjustment
        
        # Z-Score < -entry_std → 买入（超卖）
        if zscore < -entry_threshold and self._last_signal != Side.BUY:
            self._last_signal = Side.BUY
            strength = min(1.0, abs(zscore) / entry_threshold)
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=strength
            )
        
        # Z-Score > exit_std → 卖出（超买回归）
        if zscore > exit_threshold and self._last_signal == Side.BUY:
            self._last_signal = Side.SELL
            strength = min(1.0, zscore / exit_threshold)
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=strength
            )
        
        # Z-Score > entry_std → 卖出（超买）
        if zscore > entry_threshold and self._last_signal != Side.SELL:
            self._last_signal = Side.SELL
            strength = min(1.0, zscore / entry_threshold)
            return Signal(
                timestamp=bar.timestamp,
                side=Side.SELL,
                strength=strength
            )
        
        # Z-Score < -exit_std → 买入（超卖回归）
        if zscore < -exit_threshold and self._last_signal == Side.SELL:
            self._last_signal = Side.BUY
            strength = min(1.0, abs(zscore) / exit_threshold)
            return Signal(
                timestamp=bar.timestamp,
                side=Side.BUY,
                strength=strength
            )
        
        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._closes.clear()
        self._bars.clear()
        self._last_signal = None
        self._last_zscore = None

    def get_current_zscore(self) -> Optional[dict]:
        """获取当前 Z-Score 信息"""
        zscore_data = self._calculate_zscore()
        if zscore_data is None:
            return None
        
        zscore, mean, std = zscore_data
        return {
            'zscore': zscore,
            'mean': mean,
            'std': std,
            'entry_threshold': self.entry_std,
            'exit_threshold': self.exit_std,
        }
