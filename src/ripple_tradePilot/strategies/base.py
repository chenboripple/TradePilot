from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Optional

from ripple_tradePilot.models.types import Bar, Signal


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal:
        """Process a single bar and return a trading signal."""
        raise NotImplementedError

    def warmup(self, history: Iterable[Bar]) -> None:
        """Optional warmup with historical bars."""
        for bar in history:
            _ = self.on_bar(bar)

    def reset(self) -> None:
        """Reset internal state if needed."""
        pass
