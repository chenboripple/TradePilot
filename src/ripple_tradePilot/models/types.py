from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class FuturesDirection(str, Enum):
    """期货多空方向。

    P1 无账户持仓上下文，信号只表达**倾向**（做多倾向/做空倾向/观望），
    不构成可执行委托；开平仓动作等订单语义在 P2 引入。
    """

    LONG = "LONG"
    SHORT = "SHORT"


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
