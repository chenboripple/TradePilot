"""
统一回测引擎（唯一撮合实现）

撮合假设（明确声明，避免隐性偏差）：
- 默认 ``execution="next_open"``：信号在第 i 根 bar 收盘后产生，
  在第 i+1 根 bar 的开盘价成交 —— 因果正确，贴近实盘。
  ``execution="close"`` 为信号当根收盘价成交（研究模式，偏乐观，
  仅用于与旧结果对照，不应作为决策依据）。
- 涨跌停约束：开盘价已触及涨停 → 无法买入；已触及跌停 → 无法卖出。
- 成交单位为 100 股整数倍（A 股一手）。
- 成本模型：佣金（双边，默认万三，最低 5 元）+ 卖出印花税（默认万五）
  + 滑点（按成交金额比例，默认千一）。
- 风控回撤闸门触发后停止开新仓，而不是截断回测时间线；
  持仓仍按止损/止盈规则退出，权益曲线完整记录。

与旧实现的差异（修复项）：
- 卖出收入 ``cash += proceeds - fee``（旧版为覆盖写，部分仓位下现金被清零）
- 权益曲线逐 bar 用真实仓位计算（旧版用期末仓位重建，回撤/夏普恒为 0）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from ripple_tradePilot.models.types import Bar, Fill, Side
from ripple_tradePilot.risk.manager import RiskConfig, RiskManager
from ripple_tradePilot.strategies.base import Strategy

LOT_SIZE = 100          # A 股一手
MIN_FEE = 5.0           # 单笔最低佣金（元）
PRICE_LIMIT_PCT = 0.10  # 主板涨跌幅限制（简化，未区分创业板/ST 20%/5%）


@dataclass
class BacktestResult:
    equity_curve: List[float]
    fills: List[Fill]
    halted_by_drawdown: bool = False
    skipped_fills: List[dict] = field(default_factory=list)  # 涨跌停等无法成交记录


def _buy_cost(quantity: int, price: float, fee_rate: float) -> float:
    return max(MIN_FEE, quantity * price * fee_rate)


def _sell_cost(quantity: int, price: float, fee_rate: float, stamp_duty: float) -> float:
    return max(MIN_FEE, quantity * price * fee_rate) + quantity * price * stamp_duty


def _at_limit_up(price: float, prev_close: Optional[float]) -> bool:
    return prev_close is not None and price >= prev_close * (1 + PRICE_LIMIT_PCT) * 0.998


def _at_limit_down(price: float, prev_close: Optional[float]) -> bool:
    return prev_close is not None and price <= prev_close * (1 - PRICE_LIMIT_PCT) * 1.002


def run_backtest(
    strategy: Strategy,
    bars: Iterable[Bar],
    initial_cash: float = 100000.0,
    fee_rate: float = 0.0003,
    stamp_duty: float = 0.0005,
    slippage: float = 0.001,
    execution: str = "next_open",
    risk_config: RiskConfig | None = None,
) -> BacktestResult:
    """对单一标的运行回测，返回权益曲线与成交明细。

    Args:
        strategy: 流式策略实例（引擎负责逐 bar 喂入，策略只看到历史）。
        bars: 按时间升序的日线/分钟线序列。
        execution: ``"next_open"``（默认，因果正确）或 ``"close"``（当根收盘，偏乐观）。
        fee_rate: 佣金率（买卖双边），默认万三。
        stamp_duty: 印花税率（仅卖出），默认万五。
        slippage: 滑点率，买入加价/卖出降价，默认千一。
    """
    if execution not in ("next_open", "close"):
        raise ValueError(f"unsupported execution mode: {execution}")

    bar_list = list(bars)
    cash = initial_cash
    position = 0
    equity_curve: List[float] = []
    fills: List[Fill] = []
    skipped: List[dict] = []
    risk = RiskManager(risk_config or RiskConfig())
    halted = False          # 回撤闸门触发后不再开新仓
    pending: Optional[Side] = None   # next_open 模式下待执行的信号

    for index, bar in enumerate(bar_list):
        prev_close = bar_list[index - 1].close if index > 0 else None

        # ---- 1. 执行上一根 bar 产生的信号（次日开盘成交） ----
        if execution == "next_open" and pending is not None and index > 0:
            side = pending
            pending = None
            fill_price = bar.open * (1 + slippage) if side == Side.BUY else bar.open * (1 - slippage)
            blocked = False
            if side == Side.BUY:
                if _at_limit_up(bar.open, prev_close):
                    blocked = True
            elif _at_limit_down(bar.open, prev_close):
                blocked = True

            if blocked:
                skipped.append({
                    "timestamp": bar.timestamp,
                    "side": side.value,
                    "reason": "涨停无法买入" if side == Side.BUY else "跌停无法卖出",
                })
            elif side == Side.BUY and position == 0 and not halted:
                equity_now = cash
                budget = min(cash, risk.cap_position(equity_now))
                quantity = int(budget / (fill_price * (1 + fee_rate)) // LOT_SIZE) * LOT_SIZE
                if quantity > 0:
                    fee = _buy_cost(quantity, fill_price, fee_rate)
                    cash -= quantity * fill_price + fee
                    position = quantity
                    fills.append(Fill(bar.timestamp, Side.BUY, quantity, fill_price, fee))
                    risk.set_entry(fill_price)
            elif side == Side.SELL and position > 0:
                proceeds = position * fill_price
                fee = _sell_cost(position, fill_price, fee_rate, stamp_duty)
                cash += proceeds - fee
                fills.append(Fill(bar.timestamp, Side.SELL, position, fill_price, fee))
                position = 0
                risk.clear_entry()

        # ---- 2. 权益与风控状态 ----
        equity = cash + position * bar.close
        risk.update_equity(equity)
        if risk.check_drawdown(equity):
            halted = True

        # ---- 3. 持仓风控退出（止损/止盈，按收盘价决策） ----
        if position > 0 and (risk.should_stop_loss(bar.close) or risk.should_take_profit(bar.close)):
            if execution == "close":
                fill_price = bar.close * (1 - slippage)
                proceeds = position * fill_price
                fee = _sell_cost(position, fill_price, fee_rate, stamp_duty)
                cash += proceeds - fee
                fills.append(Fill(bar.timestamp, Side.SELL, position, fill_price, fee))
                position = 0
                risk.clear_entry()
            else:
                pending = Side.SELL  # 次日开盘执行

        # ---- 4. 策略信号 ----
        if position == 0 or pending != Side.SELL:
            signal = strategy.on_bar(bar)
        else:
            # 已决定离场时不再叠加反向开仓信号
            strategy.on_bar(bar)
            signal = None

        if signal is not None and signal.side is not None:
            if execution == "close":
                if signal.side == Side.BUY and position == 0 and not halted:
                    fill_price = bar.close * (1 + slippage)
                    budget = min(cash, risk.cap_position(cash))
                    quantity = int(budget / (fill_price * (1 + fee_rate)) // LOT_SIZE) * LOT_SIZE
                    if quantity > 0:
                        fee = _buy_cost(quantity, fill_price, fee_rate)
                        cash -= quantity * fill_price + fee
                        position = quantity
                        fills.append(Fill(bar.timestamp, Side.BUY, quantity, fill_price, fee))
                        risk.set_entry(fill_price)
                elif signal.side == Side.SELL and position > 0:
                    fill_price = bar.close * (1 - slippage)
                    proceeds = position * fill_price
                    fee = _sell_cost(position, fill_price, fee_rate, stamp_duty)
                    cash += proceeds - fee
                    fills.append(Fill(bar.timestamp, Side.SELL, position, fill_price, fee))
                    position = 0
                    risk.clear_entry()
            elif signal.side == Side.BUY and position == 0 and not halted:
                pending = Side.BUY
            elif signal.side == Side.SELL and position > 0 and pending != Side.SELL:
                pending = Side.SELL

        # ---- 5. 逐 bar 记录真实权益 ----
        equity = cash + position * bar.close
        equity_curve.append(equity)

    return BacktestResult(
        equity_curve=equity_curve,
        fills=fills,
        halted_by_drawdown=halted,
        skipped_fills=skipped,
    )
