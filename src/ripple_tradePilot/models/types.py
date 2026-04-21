from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    timestamp: datetime
    side: Optional[Side]  # None = no action
    strength: float = 1.0


@dataclass(frozen=True)
class Order:
    timestamp: datetime
    side: Side
    quantity: float
    price: Optional[float] = None  # market if None


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    side: Side
    quantity: float
    price: float
    fee: float
