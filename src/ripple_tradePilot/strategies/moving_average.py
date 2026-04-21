from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from ripple_tradePilot.models.types import Bar, Signal, Side
from ripple_tradePilot.strategies.base import Strategy


class MovingAverageCross(Strategy):
    name = "ma_cross"

    def __init__(self, fast: int = 5, slow: int = 20):
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self.fast = fast
        self.slow = slow
        self._fast_window: Deque[float] = deque(maxlen=fast)
        self._slow_window: Deque[float] = deque(maxlen=slow)
        self._last_side: Optional[Side] = None

    def on_bar(self, bar: Bar) -> Signal:
        self._fast_window.append(bar.close)
        self._slow_window.append(bar.close)
        if len(self._slow_window) < self.slow:
            return Signal(timestamp=bar.timestamp, side=None)

        fast_ma = sum(self._fast_window) / len(self._fast_window)
        slow_ma = sum(self._slow_window) / len(self._slow_window)

        if fast_ma > slow_ma and self._last_side != Side.BUY:
            self._last_side = Side.BUY
            return Signal(timestamp=bar.timestamp, side=Side.BUY)
        if fast_ma < slow_ma and self._last_side != Side.SELL:
            self._last_side = Side.SELL
            return Signal(timestamp=bar.timestamp, side=Side.SELL)

        return Signal(timestamp=bar.timestamp, side=None)

    def reset(self) -> None:
        self._fast_window.clear()
        self._slow_window.clear()
        self._last_side = None
