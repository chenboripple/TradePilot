"""期货回测撮合引擎（roadmap P2）。

钉死口径（每条都能在 docs/futures-roadmap.md P2 找到对应行）：

- **信号后下一可交易时点成交**：信号在已完成 K 线收盘产生，成交在该合约
  **下一根 K 线开盘价 ± 滑点**（保守且可复现；不存在「同根 bar 收盘价成交」
  的前视路径）。
- **真实合约撮合**：每张订单属于一个具体合约；换月 = 显式「平旧开新」两组
  交易，成本单列。连续拼接序列不进引擎。
- **不假定必然成交**：涨跌停（对比昨结算价 ± limit_pct）、单 bar 成交量约束、
  费用/保证金规则缺失、可用资金不足，都会拒单或部分成交；普通订单余量当 bar
  撤销，强平订单跨 bar 重试到数据结束。
- **账务全部走** :class:`~ripple_tradePilot.backtest.futures_account.FuturesAccount`
  （逐日盯市 + 成本重置），本模块只负责时点、价格与约束，绝不自算持仓盈亏。

结算价来源：日线 ``settle`` 列（P0 已核验新浪口径）；缺失记入当日
``settlement_errors``，绝不拿收盘价冒充结算价。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..models.types import FuturesDirection, FuturesOffset, FuturesOrderStatus
from ..signals.futures_eval import (
    TILT_LONG,
    TILT_NEUTRAL,
    TILT_SHORT,
    DonchianParams,
    tilt_series,
)
from .futures_account import FuturesAccount, FuturesFill
from .futures_rules import FeeSchedule, MarginSchedule, fee_for, margin_for

__all__ = [
    "ContractInput",
    "EngineConfig",
    "FuturesBacktestReport",
    "TiltSignal",
    "build_roll_schedule",
    "donchian_tilt_signals",
    "run_futures_backtest",
]

#: 主力判定 tiebreak 用的「到期远近」排序值上限（解析失败排最后）。
_EXPIRY_UNKNOWN = 999999


@dataclass
class ContractInput:
    """一个真实合约的回测输入（60m 用于信号/成交时点，日线用于结算与主力判定）。"""

    symbol: str                       # canonical，如 "RB2610.SHFE"
    multiplier: float
    tick_size: float
    bars_60m: List[Mapping[str, object]]   # 升序：trade_date, bar_time, OHLC, volume
    daily: List[Mapping[str, object]]      # 升序：trade_date, settle（可缺）, volume, hold


@dataclass(frozen=True)
class EngineConfig:
    initial_cash: float = 200000.0
    lots_per_signal: int = 1
    slippage_ticks: int = 1
    # 单 bar 成交量上限占比（成交量约束）；None = 不启用
    max_volume_participation: Optional[float] = 0.1
    limit_pct: Optional[float] = None       # 涨跌停幅度（如 0.07）；None = 不启用
    run_id: str = ""                        # 空 → 由合约集自动生成


@dataclass(frozen=True)
class TiltSignal:
    """倾向事件（P1 三态语义的时点化）：bar_index 收盘产生 → 下一根成交。

    direction 允许 NEUTRAL（目标持仓归零 = 平仓倾向）；symbol 必须是信号产生
    时点的主力合约（连续拼接序列的信号不进引擎）。
    """

    symbol: str
    bar_index: int
    direction: str                          # TILT_LONG / TILT_SHORT / TILT_NEUTRAL


@dataclass
class _Order:
    """引擎内部订单（落库时转 futures_orders 行）。"""

    id: int
    symbol: str
    direction: FuturesDirection
    offset: FuturesOffset
    lots: int
    trade_date: str = ""
    bar_time: str = ""                      # 触发时点（审计）
    status: FuturesOrderStatus = FuturesOrderStatus.PENDING
    filled_lots: int = 0
    fill_value: float = 0.0                 # Σ price×lots（加权均价用）
    reason: str = ""
    is_roll: bool = False                   # 换月腿
    is_forced: bool = False                 # 保证金不足强平 / 数据结束强平腿

    @property
    def is_open(self) -> bool:
        return self.offset == FuturesOffset.OPEN

    @property
    def avg_fill_price(self) -> Optional[float]:
        return self.fill_value / self.filled_lots if self.filled_lots else None


@dataclass
class FuturesBacktestReport:
    run_id: str
    config_echo: Dict[str, object]
    equity_curve: List[Tuple[str, float]] = field(default_factory=list)  # (trade_date, 结算后 balance)
    orders: List[_Order] = field(default_factory=list)
    trades: List[Dict[str, object]] = field(default_factory=list)
    daily: List[Dict[str, object]] = field(default_factory=list)
    rolls: List[Dict[str, object]] = field(default_factory=list)
    metrics: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def order_rows(self, run_id: str) -> List[Dict[str, object]]:
        """落库 futures_orders 的行（审计：含被拒/被撤的完整生命周期）。"""
        return [
            {
                "id": order.id, "run_id": run_id, "symbol": order.symbol,
                "direction": order.direction.value,
                "open_close": order.offset.value, "lots": order.lots,
                "price": None,  # 市价语义：触发后下一开盘撮合，无委托价
                "status": order.status.value, "filled_lots": order.filled_lots,
                "avg_fill_price": order.avg_fill_price,
                "trade_date": order.trade_date, "bar_time": order.bar_time,
                "reason": order.reason,
            }
            for order in self.orders
        ]

    def trade_rows(self, run_id: str) -> List[Dict[str, object]]:
        for row in self.trades:
            row["run_id"] = run_id
        return self.trades

    def daily_rows(self, run_id: str) -> List[Dict[str, object]]:
        for row in self.daily:
            row["run_id"] = run_id
        return self.daily


# ── 主力序列（换月计划）───────────────────────────────────────────────

def _expiry_order(symbol: str) -> int:
    """YYMM 排序键（RB2610 → 2610；值大 = 到期更远，tiebreak 排后）。"""
    digits = "".join(ch for ch in symbol.split(".")[0] if ch.isdigit())
    if len(digits) < 3:
        return _EXPIRY_UNKNOWN
    try:
        return int(digits[-4:])
    except ValueError:
        return _EXPIRY_UNKNOWN


def build_roll_schedule(
    daily_by_symbol: Mapping[str, Sequence[Mapping[str, object]]],
) -> List[Tuple[str, str]]:
    """逐交易日主力序列（P0 §5 口径，无前视）。

    主力(D) = D 之前最近一个交易日上成交量最大的合约（量同 → 持仓量大者，
    再同 → 到期更远者）。首日无前史，用首日自身数据自举（bootstrap；越靠前的
    日期「证据日」与「生效日」越接近，属近似口径，调用方应在报告标注）。
    """
    dates = sorted({
        str(row.get("trade_date", ""))
        for rows in daily_by_symbol.values() for row in rows
        if str(row.get("trade_date", ""))
    })
    if not dates:
        return []
    volume_on: Dict[Tuple[str, str], float] = {}
    hold_on: Dict[Tuple[str, str], float] = {}
    for symbol, rows in daily_by_symbol.items():
        for row in rows:
            key = (symbol, str(row.get("trade_date", "")))
            volume_on[key] = float(row.get("volume") or 0)
            hold_on[key] = float(row.get("hold") or 0)

    schedule: List[Tuple[str, str]] = []
    for index, date in enumerate(dates):
        # 无前视：判 D 的主力只用严格早于 D 的数据；首日自举用当日
        decision_date = dates[index - 1] if index > 0 else date
        candidates = [
            symbol for symbol in daily_by_symbol
            if (symbol, decision_date) in volume_on
        ] or list(daily_by_symbol)
        main = max(
            candidates,
            key=lambda s: (volume_on.get((s, decision_date), 0),
                           hold_on.get((s, decision_date), 0),
                           _expiry_order(s)),
        )
        schedule.append((date, main))
    return schedule


def donchian_tilt_signals(
    symbol: str,
    bars: List[dict],
    params: Optional[DonchianParams] = None,
) -> List[TiltSignal]:
    """把 Donchian 倾向转成**方向变化事件**序列（状态 → 边沿，引擎消费边沿）。

    每 bar 的倾向由 :func:`tilt_series` 一次算完（与 P1 监控的
    :func:`evaluate_tilt` 逐位同口径，parity 测试钉死），只保留 tilt 相对
    上一事件变化的 bar——「维持原倾向」不产生新订单。
    """
    params = params or DonchianParams()
    signals: List[TiltSignal] = []
    last: Optional[str] = None
    for index, tilt in enumerate(tilt_series(bars, params, symbol=symbol)):
        if tilt is None:
            continue  # 数据不足 = 没法算（不是观望）
        if tilt.tilt != last:
            signals.append(TiltSignal(symbol=symbol, bar_index=index, direction=tilt.tilt))
            last = tilt.tilt
    return signals


# ── 价格工具 ─────────────────────────────────────────────────────────

def _tick_ceil(price: float, tick: float) -> float:
    """向上取整到 tick 网格（买方吃亏方向，滑点保守口径）。"""
    if tick <= 0:
        return price
    return round(math.ceil(price / tick - 1e-9) * tick, 10)


def _tick_floor(price: float, tick: float) -> float:
    """向下取整到 tick 网格（卖方吃亏方向）。"""
    if tick <= 0:
        return price
    return round(math.floor(price / tick + 1e-9) * tick, 10)


def _tick_nearest(price: float, tick: float) -> float:
    """四舍五入到 tick 网格（交易所涨跌停板价的取整口径）。"""
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 10)


def _slippage_price(base: float, tick: float, slippage_ticks: int, *, is_buy: bool) -> float:
    return _tick_ceil(base + slippage_ticks * tick, tick) if is_buy \
        else _tick_floor(base - slippage_ticks * tick, tick)


# ── 主循环 ───────────────────────────────────────────────────────────

def run_futures_backtest(
    contracts: Dict[str, ContractInput],
    config: EngineConfig,
    fee_schedule: FeeSchedule,
    margin_schedule: MarginSchedule,
    signals: Sequence[TiltSignal],
    roll_schedule: Optional[Sequence[Tuple[str, str]]] = None,
) -> FuturesBacktestReport:
    """跑一次期货回测，返回可完整落库重放的报告（订单/成交/账户逐日快照）。

    每个交易日的事件顺序：结算上一日（含强平触发）→ 换月（先平后开）→
    逐 bar 撮合到期订单与信号订单；同一时点**平仓单先于开仓单**执行（先释放
    保证金再占用，避免换月被虚假的保证金不足拦住）。数据耗尽仍有持仓 → 以
    该合约最后一根收盘价 ± 滑点强平（reason=end_of_data），保证报告权益
    全部已实现、回撤与收益不含未结算浮盈。
    """
    if not contracts:
        raise ValueError("contracts 不能为空")
    run_id = config.run_id or f"fbt-{'-'.join(sorted(contracts))}"
    report = FuturesBacktestReport(
        run_id=run_id,
        config_echo={
            "initial_cash": config.initial_cash,
            "lots_per_signal": config.lots_per_signal,
            "slippage_ticks": config.slippage_ticks,
            "max_volume_participation": config.max_volume_participation,
            "limit_pct": config.limit_pct,
        },
    )
    account = FuturesAccount.create(config.initial_cash)
    multipliers = {symbol: data.multiplier for symbol, data in contracts.items()}

    bars: Dict[str, List[Mapping[str, object]]] = {
        symbol: sorted(data.bars_60m,
                       key=lambda row: (str(row.get("trade_date", "")),
                                        str(row.get("bar_time", ""))))
        for symbol, data in contracts.items()
    }
    # 结算价查表（涨跌停基准 = 昨结算价，也从此表回溯）
    settle_on: Dict[Tuple[str, str], Optional[float]] = {}
    daily_dates: Dict[str, List[str]] = {}
    for symbol, data in contracts.items():
        rows = sorted(data.daily, key=lambda row: str(row.get("trade_date", "")))
        daily_dates[symbol] = [str(row.get("trade_date", "")) for row in rows]
        for row in rows:
            settle_on[(symbol, str(row.get("trade_date", "")))] = (
                float(row["settle"]) if row.get("settle") is not None else None
            )

    def prev_settle(symbol: str, trade_date: str) -> Optional[float]:
        """严格早于 trade_date 的最近一个有结算价的日线值。"""
        best: Optional[float] = None
        for date in daily_dates.get(symbol, []):
            if date >= trade_date:
                break
            value = settle_on.get((symbol, date))
            if value is not None:
                best = value
        return best

    events: List[Tuple[str, str, str, int]] = sorted(
        (str(bar.get("trade_date", "")), str(bar.get("bar_time", "")), symbol, index)
        for symbol, rows in bars.items() for index, bar in enumerate(rows)
    )
    signals_by_point = {(s.symbol, s.bar_index): s for s in signals}
    pending: Dict[Tuple[str, int], List[_Order]] = {}
    orders: List[_Order] = []
    order_seq = [0]

    last_close: Dict[str, float] = {}
    last_rate: Optional[float] = None       # 结算率缺失时的近似回退（开仓仍严格拒绝）
    main_by_date = dict(roll_schedule or [])
    active_main: Optional[str] = None
    forced_retry: List[_Order] = []

    # ── 内部工具 ────────────────────────────────────────────────────

    def new_order(symbol: str, direction: FuturesDirection, offset: FuturesOffset,
                  lots: int, trade_date: str, bar_time: str, *,
                  is_roll: bool = False, is_forced: bool = False) -> _Order:
        order_seq[0] += 1
        order = _Order(order_seq[0], symbol, direction, offset, lots,
                       trade_date, bar_time, is_roll=is_roll, is_forced=is_forced)
        orders.append(order)
        return order

    def queue(order: _Order, fill_at: Tuple[str, int]) -> None:
        pending.setdefault(fill_at, []).append(order)

    def total_lots(symbol: str, direction: FuturesDirection) -> int:
        position = account.position(symbol, direction)
        return 0 if position is None else position.yesterday_lots + position.today_lots

    def close_orders_for(symbol: str, trade_date: str, bar_time: str,
                         lots: Optional[int], *, is_roll: bool = False,
                         is_forced: bool = False) -> List[_Order]:
        """对某合约持仓建平仓单，按今昨仓拆分（两档费用口径不同）。"""
        created: List[_Order] = []
        for direction in (FuturesDirection.LONG, FuturesDirection.SHORT):
            held = total_lots(symbol, direction)
            remaining = held if lots is None else min(lots, held)
            for offset in (FuturesOffset.CLOSE_YESTERDAY, FuturesOffset.CLOSE_TODAY):
                take = min(account.closeable_lots(symbol, direction, offset, trade_date),
                           remaining)
                if take <= 0:
                    continue
                created.append(new_order(symbol, direction, offset, take,
                                         trade_date, bar_time,
                                         is_roll=is_roll, is_forced=is_forced))
                remaining -= take
        return created

    def first_bar_on_or_after(symbol: str, trade_date: str) -> Optional[int]:
        for index, bar in enumerate(bars.get(symbol, [])):
            if str(bar.get("trade_date", "")) >= trade_date:
                return index
        return None

    def settle_date(trade_date: str) -> None:
        """收盘逐日结算 + 账户快照 + 保证金不足 → 强平订单。"""
        nonlocal last_rate
        rate = margin_schedule.rate_on(trade_date)
        if rate is not None:
            last_rate = rate
        settle_prices = {}
        for symbol in contracts:
            if total_lots(symbol, FuturesDirection.LONG) or \
                    total_lots(symbol, FuturesDirection.SHORT):
                value = settle_on.get((symbol, trade_date))
                if value is not None:
                    settle_prices[symbol] = value
        errors: List[str] = []
        position_pnl = 0.0
        effective_rate = rate if rate is not None else last_rate
        if effective_rate is None:
            # 连近似回退都没有：宁可跳过结算并在快照记错，也不编造保证金率
            errors.append(f"{trade_date}:margin_rate_missing")
            report.warnings.append(f"{trade_date} 无保证金率，当日未结算")
        else:
            result = account.settle(trade_date, settle_prices, multipliers,
                                    effective_rate)
            position_pnl = result.position_pnl
            errors = [f"{trade_date}:{symbol}" for symbol in result.errors]
        exposure = sum(
            abs(last_close.get(symbol, 0.0) * contracts[symbol].multiplier *
                (total_lots(symbol, FuturesDirection.LONG)
                 + total_lots(symbol, FuturesDirection.SHORT)))
            for symbol in contracts
        )
        report.equity_curve.append((trade_date, account.balance))
        report.daily.append({
            "run_id": run_id, "trade_date": trade_date,
            "balance": account.balance, "equity": account.balance,
            "available": account.balance - account.margin_occupied,
            "margin_occupied": account.margin_occupied,
            "realized_pnl_today": account.realized_today(trade_date),
            "position_pnl_today": position_pnl,
            "fees_today": account.fees_on(trade_date),
            "exposure_value": exposure,
            # 无错存空串（metrics 与 UI 用真值判断即可区分，不解析 JSON）
            "settlement_errors": json.dumps(errors, ensure_ascii=False) if errors else "",
        })
        # 结算后可用资金为负 → 下一交易日强平全部持仓（受成交约束，跨 bar 重试）
        if account.balance - account.margin_occupied < 0:
            report.warnings.append(
                f"{trade_date} 结算后可用资金为负，触发强平")
            for symbol in contracts:
                for direction in (FuturesDirection.LONG, FuturesDirection.SHORT):
                    held = total_lots(symbol, direction)
                    if held > 0:
                        forced_retry.extend(close_orders_for(
                            symbol, trade_date, "", held, is_forced=True))

    # ── 撮合 ────────────────────────────────────────────────────────

    def _fail(order: _Order, reason: str) -> None:
        """订单受挫：强平单跨 bar 重试（no_position 除外——没仓可平，重试无意义）；
        普通单终态（拒单/撤单）。"""
        order.reason = reason
        if (order.is_forced and reason != "no_position"
                and order.filled_lots < order.lots):
            forced_retry.append(order)
            return
        order.status = (FuturesOrderStatus.REJECTED if order.filled_lots == 0
                        else FuturesOrderStatus.CANCELLED)

    def _execute(order: _Order, symbol: str, bar_index: int, trade_date: str) -> None:
        bar = bars[symbol][bar_index]
        _execute_at_price(order, float(bar.get("open") or 0.0), contracts[symbol],
                          trade_date, bar_time=str(bar.get("bar_time", "")),
                          bar_volume=float(bar.get("volume") or 0))

    def _execute_at_price(order: _Order, base_price: float, data: ContractInput,
                          trade_date: str, bar_time: str = "",
                          bar_volume: Optional[float] = None) -> None:
        """以 base_price ± 滑点撮合一张订单（涨跌停/量约束/规则/资金 gate 全在此）。"""
        order.trade_date = trade_date
        if bar_time:
            order.bar_time = bar_time
        is_buy = (
            (order.direction is FuturesDirection.LONG and order.is_open) or
            (order.direction is FuturesDirection.SHORT and not order.is_open)
        )
        price = _slippage_price(base_price, data.tick_size, config.slippage_ticks,
                                is_buy=is_buy)

        # ⓪ 平仓单改道与限仓：订单可能跨日存续（信号 bar 与成交 bar 隔日），
        #    下单时的今仓在结算后已毕业为昨仓——平今/平昨按**成交时点**的可平
        #    拆分自动改道，费用与盈亏口径随之取对（不猜、不沿用陈旧档位）。
        want = order.lots - order.filled_lots
        if not order.is_open:
            held = account.closeable_lots(order.symbol, order.direction, order.offset,
                                          trade_date)
            if held == 0:
                alts = ([FuturesOffset.CLOSE_YESTERDAY, FuturesOffset.CLOSE_TODAY]
                        if order.offset is FuturesOffset.CLOSE_TODAY
                        else [FuturesOffset.CLOSE_TODAY, FuturesOffset.CLOSE_YESTERDAY])
                for alt in alts:
                    alt_held = account.closeable_lots(order.symbol, order.direction,
                                                      alt, trade_date)
                    if alt_held > 0:
                        order.offset = alt
                        held = alt_held
                        break
            if held == 0:
                _fail(order, "no_position")
                return
            want = min(want, held)

        # ① 涨跌停：成交价达到板价即拒（不假定涨停板上能买到/跌停板上能卖出）
        if config.limit_pct is not None:
            previous = prev_settle(order.symbol, trade_date)
            if previous is not None:
                limit_up = _tick_nearest(previous * (1 + config.limit_pct), data.tick_size)
                limit_down = _tick_nearest(previous * (1 - config.limit_pct), data.tick_size)
                if is_buy and price >= limit_up:
                    _fail(order, "limit_up")
                    return
                if not is_buy and price <= limit_down:
                    _fail(order, "limit_down")
                    return

        # ② 成交量约束：单 bar 最多吃 floor(volume × participation)
        if config.max_volume_participation is not None and bar_volume is not None:
            want = min(want, int(bar_volume * config.max_volume_participation))
        if want <= 0:
            _fail(order, "volume_cap")
            return

        # ③ 费用规则：开仓缺失 → 拒单（规则缺失禁止成交）；平仓缺失 → 记 0 并
        #    标注近似（持仓必须可平，不许因缺费率把仓位困死在账户里）
        rule = fee_schedule.rule_on(trade_date)
        fee = fee_for(rule, order.offset, price, want, data.multiplier) if rule else None
        if fee is None:
            if order.is_open:
                _fail(order, "fee_missing")
                return
            fee = 0.0
            report.warnings.append(
                f"{trade_date} {order.symbol} 平仓缺费用规则，按 0 计（近似口径）")

        # ④ 保证金（仅开仓）：当日规则缺失或可用资金不足 → 拒单
        rate = margin_schedule.rate_on(trade_date)
        if order.is_open:
            if rate is None:
                _fail(order, "margin_rule_missing")
                return
            required = margin_for(rate, price, want, data.multiplier)
            marks = dict(last_close)
            marks[order.symbol] = price
            if account.available(marks) < required + fee:
                _fail(order, "margin_shortfall")
                return

        balance_before = account.balance
        fill = FuturesFill(
            order_id=str(order.id), symbol=order.symbol, direction=order.direction,
            offset=order.offset, price=price, lots=want, fee=fee,
            trade_date=trade_date, bar_time=bar_time,
        )
        breakdown = account.apply_fill(
            fill, data.multiplier,
            rate if rate is not None else (last_rate or 0.0))
        order.filled_lots += want
        order.fill_value += price * want
        realized = (account.balance - balance_before) + fee  # balance 变动 = realized − fee
        report.trades.append({
            "run_id": run_id, "order_id": str(order.id), "symbol": order.symbol,
            "direction": order.direction.value, "open_close": order.offset.value,
            "price": price, "lots": want, "fee": fee,
            "trade_date": trade_date, "bar_time": bar_time,
            "realized_pnl": None if order.is_open else realized,
            "close_from_yesterday": None if order.is_open else breakdown.from_yesterday,
            "close_from_today": None if order.is_open else breakdown.from_today,
            "is_roll": order.is_roll, "is_forced": order.is_forced,
        })
        if order.filled_lots >= order.lots:
            order.status = FuturesOrderStatus.FILLED
        elif order.is_forced:
            forced_retry.append(order)      # 强平余量下一根 bar 继续
        else:
            order.status = FuturesOrderStatus.CANCELLED  # 普通订单余量当 bar 放弃
            order.reason = order.reason or "volume_cap"

    def _signal_orders(signal: TiltSignal, trade_date: str,
                       bar_time: str) -> List[_Order]:
        """倾向事件 → 目标持仓订单（无持仓上下文的倾向到 P2 才转成动作，就在这里）。"""
        symbol, direction = signal.symbol, signal.direction
        created: List[_Order] = []
        want_long = direction == TILT_LONG
        want_short = direction == TILT_SHORT
        for side, wanted in ((FuturesDirection.LONG, want_long),
                             (FuturesDirection.SHORT, want_short)):
            held = total_lots(symbol, side)
            if held > 0 and not wanted:
                created.extend(close_orders_for(symbol, trade_date, bar_time, held))
        if want_long and total_lots(symbol, FuturesDirection.LONG) == 0:
            created.append(new_order(symbol, FuturesDirection.LONG, FuturesOffset.OPEN,
                                     config.lots_per_signal, trade_date, bar_time))
        elif want_short and total_lots(symbol, FuturesDirection.SHORT) == 0:
            created.append(new_order(symbol, FuturesDirection.SHORT, FuturesOffset.OPEN,
                                     config.lots_per_signal, trade_date, bar_time))
        return created  # NEUTRAL 只平不开

    # ── 逐事件推进 ──────────────────────────────────────────────────
    processed_dates: List[str] = []
    for trade_date, bar_time, symbol, bar_index in events:
        if not processed_dates or processed_dates[-1] != trade_date:
            if processed_dates:
                settle_date(processed_dates[-1])   # 新交易日开盘前结算上一日
            processed_dates.append(trade_date)
            # 上一日强平订单挂到本日该合约第一根 bar（跨 bar 重试）
            for order in forced_retry:
                next_index = first_bar_on_or_after(order.symbol, trade_date)
                if next_index is None:
                    order.status = FuturesOrderStatus.CANCELLED
                    order.reason = "forced_no_bar"
                else:
                    queue(order, (order.symbol, next_index))
            forced_retry = []
            # 换月：主力切换日先平旧主力、再开新主力（净持仓方向保持）
            new_main = main_by_date.get(trade_date, active_main)
            if (new_main and new_main != active_main and active_main is not None):
                net = (total_lots(active_main, FuturesDirection.LONG)
                       - total_lots(active_main, FuturesDirection.SHORT))
                closed = close_orders_for(active_main, trade_date, bar_time,
                                          None, is_roll=True)
                old_index = first_bar_on_or_after(active_main, trade_date)
                if old_index is None:
                    for order in closed:
                        order.status = FuturesOrderStatus.CANCELLED
                        order.reason = "roll_no_bar"
                    report.warnings.append(
                        f"{trade_date} 换月：旧主力 {active_main} 此后无 bar，"
                        f"平仓转入 end_of_data 兜底")
                else:
                    for order in closed:
                        queue(order, (active_main, old_index))
                new_index = first_bar_on_or_after(new_main, trade_date) \
                    if new_main in contracts else None
                if net != 0 and new_index is not None:
                    direction = FuturesDirection.LONG if net > 0 else FuturesDirection.SHORT
                    queue(new_order(new_main, direction, FuturesOffset.OPEN, abs(net),
                                    trade_date, bar_time, is_roll=True),
                          (new_main, new_index))
                elif net != 0:
                    report.warnings.append(
                        f"{trade_date} 换月：新主力 {new_main} 当日无 bar，只平不开")
                if net != 0:
                    report.rolls.append({"trade_date": trade_date, "from": active_main,
                                         "to": new_main, "net_lots": net})
            if new_main:
                active_main = new_main

        bar = bars[symbol][bar_index]
        last_close[symbol] = float(bar.get("close") or 0.0)

        # 信号在 bar 收盘产生 → 订单在下一根 bar 开盘成交
        signal = signals_by_point.get((symbol, bar_index))
        if signal is not None:
            for order in _signal_orders(signal, trade_date, bar_time):
                if bar_index + 1 < len(bars[symbol]):
                    queue(order, (symbol, bar_index + 1))
                else:
                    order.status = FuturesOrderStatus.CANCELLED
                    order.reason = "no_next_bar"

        # 本 bar 到期订单撮合：平先开后（先释放保证金）
        due = pending.pop((symbol, bar_index), [])
        due.sort(key=lambda order: 1 if order.is_open else 0)
        for order in due:
            _execute(order, symbol, bar_index, trade_date)

    # ── 数据结束：撤残单、强平残仓、结算末日 ────────────────────────
    for queued in pending.values():
        for order in queued:
            order.status = FuturesOrderStatus.CANCELLED
            order.reason = "data_end"
    pending.clear()
    for order in forced_retry:
        order.status = FuturesOrderStatus.CANCELLED
        order.reason = "data_end"
    forced_retry = []
    for symbol in contracts:
        rows = bars.get(symbol, [])
        last = rows[-1] if rows else None
        if last is None:
            continue
        for order in close_orders_for(symbol, str(last.get("trade_date", "")),
                                      str(last.get("bar_time", "")), None,
                                      is_forced=True):
            _execute_at_price(order, float(last.get("close") or 0.0), contracts[symbol],
                              str(last.get("trade_date", "")),
                              bar_time=str(last.get("bar_time", "")),
                              bar_volume=float(last.get("volume") or 0))
    if processed_dates:
        settle_date(processed_dates[-1])   # 末日持仓已强平 → 结算在空仓上是安全空转
    # 终态兜底：任何仍非终态的订单（如强平受挫残留）记为撤单，报告不留悬单
    for order in orders:
        if order.status in (FuturesOrderStatus.PENDING,
                            FuturesOrderStatus.PARTIALLY_FILLED):
            order.status = FuturesOrderStatus.CANCELLED
            order.reason = order.reason or "data_end"

    report.orders = orders
    _finalize_metrics(report, account, config, dict(last_close))
    return report


def _finalize_metrics(report: FuturesBacktestReport, account: FuturesAccount,
                      config: EngineConfig, marks: Dict[str, float]) -> None:
    final_equity = account.equity(marks)
    curve = [value for _, value in report.equity_curve]
    peak = config.initial_cash
    max_drawdown = 0.0
    for value in curve + [final_equity]:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, 1 - value / peak)
    status_counts: Dict[str, int] = {}
    for order in report.orders:
        status_counts[order.status.value] = status_counts.get(order.status.value, 0) + 1
    exposures = [float(row.get("exposure_value") or 0) for row in report.daily]
    margins = [float(row.get("margin_occupied") or 0) for row in report.daily]
    report.metrics = {
        "initial_cash": config.initial_cash,
        "final_equity": final_equity,
        "total_return": final_equity / config.initial_cash - 1
        if config.initial_cash else 0.0,
        "max_drawdown": max_drawdown,
        "fees_total": account.fees_total(),
        "fills": len(report.trades),
        "orders_by_status": status_counts,
        "rolls": len(report.rolls),
        "forced_liquidations": sum(1 for row in report.trades if row.get("is_forced")),
        "settlement_error_days": sum(
            1 for row in report.daily if row.get("settlement_errors")),
        "exposure_mean": sum(exposures) / len(exposures) if exposures else 0.0,
        "exposure_max": max(exposures) if exposures else 0.0,
        "margin_occupied_mean": sum(margins) / len(margins) if margins else 0.0,
        "margin_occupied_max": max(margins) if margins else 0.0,
    }
