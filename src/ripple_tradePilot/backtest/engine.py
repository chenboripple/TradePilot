from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from ripple_tradePilot.models.types import Bar, Fill, Side
from ripple_tradePilot.risk.manager import RiskConfig, RiskManager
from ripple_tradePilot.strategies.base import Strategy


@dataclass
class BacktestResult:
    equity_curve: List[float]
    fills: List[Fill]


def run_backtest(
    strategy: Strategy,
    bars: Iterable[Bar],
    initial_cash: float = 100000.0,
    fee_rate: float = 0.0005,
    risk_config: RiskConfig | None = None,
) -> BacktestResult:
    cash = initial_cash
    position = 0.0
    equity_curve: List[float] = []
    fills: List[Fill] = []
    risk = RiskManager(risk_config or RiskConfig())

    for bar in bars:
        equity = cash + position * bar.close
        risk.update_equity(equity)

        # risk exits
        if position > 0 and (risk.should_stop_loss(bar.close) or risk.should_take_profit(bar.close)):
            proceeds = position * bar.close
            fee = proceeds * fee_rate
            cash = proceeds - fee
            fills.append(Fill(bar.timestamp, Side.SELL, position, bar.close, fee))
            position = 0.0
            risk.clear_entry()

        if risk.check_drawdown(equity):
            break

        signal = strategy.on_bar(bar)
        if signal.side == Side.BUY and position == 0:
            max_capital = risk.cap_position(equity)
            quantity = max_capital / bar.close
            fee = max_capital * fee_rate
            cash = cash - max_capital
            position = quantity
            fills.append(Fill(bar.timestamp, Side.BUY, quantity, bar.close, fee))
            risk.set_entry(bar.close)
        elif signal.side == Side.SELL and position > 0:
            proceeds = position * bar.close
            fee = proceeds * fee_rate
            cash = proceeds - fee
            fills.append(Fill(bar.timestamp, Side.SELL, position, bar.close, fee))
            position = 0.0
            risk.clear_entry()

        equity = cash + position * bar.close
        equity_curve.append(equity)

    return BacktestResult(equity_curve=equity_curve, fills=fills)
