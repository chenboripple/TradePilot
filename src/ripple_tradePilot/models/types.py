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


class FuturesOffset(str, Enum):
    """期货开平方向（P2）。

    国内商品期货的费用与盈亏口径随开平不同：开仓、平今（平当日新开仓）、
    平昨（平历史持仓）费率可各自独立；plain CLOSE 由引擎按「昨仓优先」
    的保守可复现规则拆分（撮合层负责，账户层只接受显式四态）。
    """

    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_TODAY = "CLOSE_TODAY"
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class FuturesOrderStatus(str, Enum):
    """期货回测订单生命周期状态（P2）。

    部分成交（PARTIALLY_FILLED）是常态路径：涨跌停、成交量约束都会
    让订单只成交一部分，剩余量按撮合规则取消或挂起，不假定必然成交。
    """

    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


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
