from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ripple_tradePilot.models.types import Bar, Side


@dataclass
class RiskConfig:
    max_position_pct: float = 1.0   # 1.0 = 100% of equity
    stop_loss_pct: float = 0.08     # 8% stop loss
    take_profit_pct: float = 0.20   # 20% take profit
    max_drawdown_pct: float = 0.20  # 20% max drawdown


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self._entry_price: Optional[float] = None
        self._peak_equity: Optional[float] = None

    def update_equity(self, equity: float) -> None:
        if self._peak_equity is None:
            self._peak_equity = equity
        else:
            self._peak_equity = max(self._peak_equity, equity)

    def check_drawdown(self, equity: float) -> bool:
        if self._peak_equity is None:
            return False
        dd = (self._peak_equity - equity) / self._peak_equity
        return dd >= self.config.max_drawdown_pct

    def set_entry(self, price: float) -> None:
        self._entry_price = price

    def clear_entry(self) -> None:
        self._entry_price = None

    def should_stop_loss(self, price: float) -> bool:
        if self._entry_price is None:
            return False
        return (self._entry_price - price) / self._entry_price >= self.config.stop_loss_pct

    def should_take_profit(self, price: float) -> bool:
        if self._entry_price is None:
            return False
        return (price - self._entry_price) / self._entry_price >= self.config.take_profit_pct

    def cap_position(self, equity: float) -> float:
        return equity * self.config.max_position_pct
