"""
Trend Filter (趋势过滤) 策略

原理：
- 使用多条均线判断趋势方向
- 短期均线 > 中期均线 > 长期均线 → 上升趋势 (只允许 BUY 信号)
- 短期均线 < 中期均线 < 长期均线 → 下降趋势 (只允许 SELL 信号)
- 其他 → 震荡 (允许双向)

通常与其他策略组合使用，用于过滤逆势信号。

参数：
- short: 短期均线（默认 5）
- medium: 中期均线（默认 20）
- long: 长期均线（默认 60）
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class TrendFilter(Strategy):
    name = "trend_filter"

    def __init__(
        self,
        short: int = 5,
        medium: int = 20,
        long: int = 60,
    ):
        if short >= medium or medium >= long:
            raise ValueError("short < medium < long required")
        
        self.short = short
        self.medium = medium
        self.long = long
        
        self._short_window: Deque[float] = deque(maxlen=short)
        self._medium_window: Deque[float] = deque(maxlen=medium)
        self._long_window: Deque[float] = deque(maxlen=long)
        self._trend: Optional[str] = None  # 'up', 'down', 'neutral'

    def _calculate_trend(self) -> Optional[str]:
        """计算趋势方向"""
        if len(self._long_window) < self.long:
            return None
        
        short_ma = sum(self._short_window) / len(self._short_window)
        medium_ma = sum(self._medium_window) / len(self._medium_window)
        long_ma = sum(self._long_window) / len(self._long_window)
        
        if short_ma > medium_ma > long_ma:
            return 'up'
        elif short_ma < medium_ma < long_ma:
            return 'down'
        else:
            return 'neutral'

    def on_bar(self, bar: Bar) -> Signal:
        self._short_window.append(bar.close)
        self._medium_window.append(bar.close)
        self._long_window.append(bar.close)
        
        trend = self._calculate_trend()
        
        if trend is None:
            return Signal(timestamp=bar.timestamp, side=None)
        
        self._trend = trend
        
        # 趋势过滤本身不产生交易信号，只返回趋势状态
        # 使用 get_trend() 方法获取当前趋势
        return Signal(timestamp=bar.timestamp, side=None)

    def get_trend(self) -> Optional[str]:
        """获取当前趋势方向 ('up', 'down', 'neutral' 或 None)"""
        return self._trend

    def allow_buy(self) -> bool:
        """是否允许买入信号（上升趋势或震荡）"""
        return self._trend in ('up', 'neutral')

    def allow_sell(self) -> bool:
        """是否允许卖出信号（下降趋势或震荡）"""
        return self._trend in ('down', 'neutral')

    def reset(self) -> None:
        self._short_window.clear()
        self._medium_window.clear()
        self._long_window.clear()
        self._trend = None


class TrendFilteredStrategy(Strategy):
    """
    趋势过滤包装器
    
    将任意策略与趋势过滤结合，只在趋势允许时产生信号。
    """
    
    def __init__(self, strategy: Strategy, trend_filter: TrendFilter):
        self.strategy = strategy
        self.trend_filter = trend_filter
        self.name = f"{strategy.name}_tf"

    def on_bar(self, bar: Bar) -> Signal:
        # 先更新趋势过滤
        tf_signal = self.trend_filter.on_bar(bar)
        
        # 再更新基础策略
        base_signal = self.strategy.on_bar(bar)
        
        # 根据趋势过滤信号
        if base_signal.side == Side.BUY and not self.trend_filter.allow_buy():
            return Signal(timestamp=bar.timestamp, side=None)
        
        if base_signal.side == Side.SELL and not self.trend_filter.allow_sell():
            return Signal(timestamp=bar.timestamp, side=None)
        
        return base_signal

    def reset(self) -> None:
        self.strategy.reset()
        self.trend_filter.reset()
