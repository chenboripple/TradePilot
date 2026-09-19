"""期货账户账本（P2）：订单/持仓模型 + 逐日盯市结算的纯内存账务内核。

会计口径（国内商品期货「逐日盯市 / 单腿保证金」标准，是 roadmap P2
「固定案例逐笔账目核对」的锚，勿随意改动公式）：

- **持仓拆分**：每 (symbol, direction) 一条持仓，拆今仓（当日开仓，成本 =
  开仓加权均价）与昨仓（历史持仓，成本 = 上日结算价）。多空两侧可同时
  持仓（锁仓允许），各自独立计账、各自占保证金。
- **开仓**：margin_occupied += 开仓价 × 乘数 × 手数 × 保证金率（新仓按开仓价
  计）；balance -= fee（费用当日直接从结存扣）。
- **平仓盈亏**（进 balance）：平昨按昨结算价、平今按今开仓均价算价差 × 方向
  符号；plain CLOSE 昨仓优先，可跨两档（返回拆分明细）。释放保证金按被平
  lot 各自基准价口径。平仓手数超过可平 → ValueError（引擎部分成交前先查
  closeable_lots，账户层兜底防账目穿透）。
- **逐日结算 settle**：持仓盯市盈亏全额计入 balance（当日盯市当日兑现），随后
  成本重置为今结算价、今仓毕业并入昨仓——同一价差只计一次，**结算盯市与
  平仓盈亏不重复计入**（roadmap §P2 验收点）。缺结算价的品种记入 errors
  跳过，绝不静默用 close 价冒充结算价。
- **权益**：无 marks → balance（结算后两者相等是不变式）；有 marks →
  balance + Σ(mark − 成本) × 乘数 × 手数 × 方向符号——balance 里已含已实现
  平仓盈亏与历史盯市，盘中未实现部分只补不重。资金充足性拦截不在账本
  （撮合层 gate），账本只提供 equity/available 查询。

费用与保证金率由撮合层按 ``backtest/futures_rules.py`` 口径算好后作为参数
传入；本模块不做率值合理性校验。无网络、无 DB、无 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

from ripple_tradePilot.models.types import FuturesDirection, FuturesOffset


@dataclass(frozen=True)
class FuturesFill:
    """一笔期货成交（审计原子；费用已由撮合层按开平口径算好）。"""

    order_id: str
    symbol: str            # 规范形如 "RB2610.SHFE"
    direction: FuturesDirection
    offset: FuturesOffset
    price: float
    lots: int
    fee: float
    trade_date: str        # "YYYYMMDD"
    bar_time: str = ""     # 审计用，可为空


@dataclass
class PositionState:
    """单 (symbol, direction) 持仓的今/昨两档状态。"""

    symbol: str
    direction: FuturesDirection
    yesterday_lots: int = 0
    today_lots: int = 0
    yesterday_cost: float = 0.0   # 每手基准（上日结算价）
    today_cost: float = 0.0       # 每手开仓均价


@dataclass
class CloseBreakdown:
    """plain CLOSE 跨档拆分明细（审计与报告用）。"""

    from_yesterday: int = 0
    from_today: int = 0


@dataclass
class SettlementResult:
    """一次逐日结算的结果快照。"""

    trade_date: str
    position_pnl: float          # 当日持仓盯市盈亏
    balance_after: float
    margin_occupied_after: float
    errors: List[str] = field(default_factory=list)  # 缺结算价的持仓品种


class FuturesAccount:
    """期货账户账务内核：只管账，不撮合、不校验资金充足性。"""

    @classmethod
    def create(cls, initial_cash: float) -> "FuturesAccount":
        return cls(initial_cash)

    def __init__(self, initial_cash: float) -> None:
        self.initial_cash: float = initial_cash
        self.balance: float = initial_cash  # 动态结存 = 上日结存 + 当日已实现/已盯市
        self.margin_occupied: float = 0.0
        self.fills: List[FuturesFill] = []
        self.settlements: List[SettlementResult] = []
        self._positions: Dict[Tuple[str, FuturesDirection], PositionState] = {}
        # equity/available 按持仓现算需要乘数，但接口不传——开仓/结算时记住
        self._multipliers: Dict[str, float] = {}
        # 当日平仓盈亏合计（按 trade_date 累计，报告用）
        self._realized_by_date: Dict[str, float] = {}

    # ------------------------------------------------------------------ 查询

    def position(self, symbol: str, direction: FuturesDirection) -> Optional[PositionState]:
        """返回该持仓的快照拷贝（防外部改账本内部状态）；无持仓返回 None。"""
        pos = self._positions.get((symbol, direction))
        return replace(pos) if pos is not None else None

    def positions_snapshot(self) -> List[PositionState]:
        """全部非空持仓的快照拷贝。"""
        return [
            replace(p)
            for p in self._positions.values()
            if p.today_lots + p.yesterday_lots > 0
        ]

    def closeable_lots(
        self, symbol: str, direction: FuturesDirection, offset: FuturesOffset, trade_date: str
    ) -> int:
        """按 offset 口径的可平手数（引擎部分成交前必须先查这里）。

        trade_date 仅占位（接口对齐撮合层）：今仓在结算后才毕业为昨仓，
        本方法纯查当前状态，不跨日记忆。
        """
        pos = self._positions.get((symbol, direction))
        if pos is None or offset == FuturesOffset.OPEN:
            return 0
        if offset == FuturesOffset.CLOSE_TODAY:
            return pos.today_lots
        if offset == FuturesOffset.CLOSE_YESTERDAY:
            return pos.yesterday_lots
        return pos.today_lots + pos.yesterday_lots  # plain CLOSE：两档合计

    def realized_today(self, trade_date: str) -> float:
        """当日平仓盈亏合计（报告用；未发生平仓返回 0）。"""
        return self._realized_by_date.get(trade_date, 0.0)

    def fees_total(self) -> float:
        return sum(f.fee for f in self.fills)

    def fees_on(self, trade_date: str) -> float:
        return sum(f.fee for f in self.fills if f.trade_date == trade_date)

    def equity(self, marks: Optional[Dict[str, float]] = None) -> float:
        """权益：无 marks → balance（结算后相等是不变式）；有 marks（盘中现价）
        → balance + Σ(mark − 成本) × 乘数 × 手数 × 方向符号，今昨分批、只补不重。
        """
        if marks is None:
            return self.balance
        unrealized = 0.0
        for pos in self._positions.values():
            mark = marks.get(pos.symbol)
            multiplier = self._multipliers.get(pos.symbol)
            if mark is None or multiplier is None:
                continue  # 无现价的品种不臆造，保持 balance 口径
            sign = 1.0 if pos.direction == FuturesDirection.LONG else -1.0
            if pos.today_lots:
                unrealized += (mark - pos.today_cost) * multiplier * pos.today_lots * sign
            if pos.yesterday_lots:
                unrealized += (mark - pos.yesterday_cost) * multiplier * pos.yesterday_lots * sign
        return self.balance + unrealized

    def available(self, marks: Optional[Dict[str, float]] = None) -> float:
        """可用资金 = 权益 − 保证金占用。资金充足性 gate 在撮合层，这里只查询。"""
        return self.equity(marks) - self.margin_occupied

    # ---------------------------------------------------------------- 记账

    def apply_fill(
        self, fill: FuturesFill, multiplier: float, margin_rate: float
    ) -> CloseBreakdown:
        """把一笔成交记入账本；返回 plain CLOSE 的跨档拆分明细（开仓为零值）。

        超量平仓抛 ValueError（消息含 symbol/方向/可平手数）；lots/price 非正
        抛 ValueError。保证金率合理性由 rules 层把关，此处不校验。
        """
        if fill.lots <= 0:
            raise ValueError(f"成交手数必须为正: {fill.symbol} lots={fill.lots}")
        if fill.price <= 0:
            raise ValueError(f"成交价必须为正: {fill.symbol} price={fill.price}")
        if multiplier <= 0:
            raise ValueError(f"合约乘数必须为正: {fill.symbol} multiplier={multiplier}")

        self._multipliers[fill.symbol] = multiplier
        sign = 1.0 if fill.direction == FuturesDirection.LONG else -1.0

        if fill.offset == FuturesOffset.OPEN:
            self._apply_open(fill, multiplier, margin_rate)
            self.fills.append(fill)
            return CloseBreakdown()

        breakdown, realized = self._apply_close(fill, multiplier, margin_rate, sign)
        self.balance += realized - fill.fee
        self._realized_by_date[fill.trade_date] = (
            self._realized_by_date.get(fill.trade_date, 0.0) + realized
        )
        self.fills.append(fill)
        return breakdown

    def _apply_open(self, fill: FuturesFill, multiplier: float, margin_rate: float) -> None:
        key = (fill.symbol, fill.direction)
        pos = self._positions.get(key)
        if pos is None:
            pos = PositionState(symbol=fill.symbol, direction=fill.direction)
            self._positions[key] = pos
        # 今仓成本 = 同日多次开仓按手数加权均价
        total = pos.today_lots + fill.lots
        pos.today_cost = (pos.today_cost * pos.today_lots + fill.price * fill.lots) / total
        pos.today_lots = total
        # 新仓按开仓价计保证金
        self.margin_occupied += fill.price * multiplier * fill.lots * margin_rate
        self.balance -= fill.fee  # 费用当日直接从结存扣

    def _apply_close(
        self, fill: FuturesFill, multiplier: float, margin_rate: float, sign: float
    ) -> Tuple[CloseBreakdown, float]:
        pos = self._positions.get((fill.symbol, fill.direction))
        if pos is None:
            raise ValueError(
                f"平仓手数超过可平: {fill.symbol} {fill.direction.value}"
                f" 可平 0 手, 需平 {fill.lots} 手"
            )
        if fill.offset == FuturesOffset.CLOSE_YESTERDAY:
            if fill.lots > pos.yesterday_lots:
                raise ValueError(
                    f"平仓手数超过可平: {fill.symbol} {fill.direction.value}"
                    f" 可平 {pos.yesterday_lots} 手(昨仓), 需平 {fill.lots} 手"
                )
            from_y, from_t = fill.lots, 0
        elif fill.offset == FuturesOffset.CLOSE_TODAY:
            if fill.lots > pos.today_lots:
                raise ValueError(
                    f"平仓手数超过可平: {fill.symbol} {fill.direction.value}"
                    f" 可平 {pos.today_lots} 手(今仓), 需平 {fill.lots} 手"
                )
            from_y, from_t = 0, fill.lots
        else:  # plain CLOSE：昨仓优先，不够再平今仓（保守兜底）
            closeable = pos.yesterday_lots + pos.today_lots
            if fill.lots > closeable:
                raise ValueError(
                    f"平仓手数超过可平: {fill.symbol} {fill.direction.value}"
                    f" 可平 {closeable} 手, 需平 {fill.lots} 手"
                )
            from_y = min(fill.lots, pos.yesterday_lots)
            from_t = fill.lots - from_y

        realized = 0.0
        if from_y:
            # 平昨：价差基准 = 上日结算价；按昨口径释放保证金
            realized += (fill.price - pos.yesterday_cost) * multiplier * from_y * sign
            self.margin_occupied -= pos.yesterday_cost * multiplier * from_y * margin_rate
            pos.yesterday_lots -= from_y
            if pos.yesterday_lots == 0:
                pos.yesterday_cost = 0.0  # 清档归零，避免残留陈旧基准价
        if from_t:
            # 平今：价差基准 = 今开仓均价；按今口径释放保证金
            realized += (fill.price - pos.today_cost) * multiplier * from_t * sign
            self.margin_occupied -= pos.today_cost * multiplier * from_t * margin_rate
            pos.today_lots -= from_t
            if pos.today_lots == 0:
                pos.today_cost = 0.0

        if pos.yesterday_lots == 0 and pos.today_lots == 0:
            del self._positions[(fill.symbol, fill.direction)]  # 清仓即移除
        return CloseBreakdown(from_yesterday=from_y, from_today=from_t), realized

    def settle(
        self,
        trade_date: str,
        settle_prices: Dict[str, float],
        multipliers: Dict[str, float],
        margin_rate: float,
    ) -> SettlementResult:
        """逐日盯市结算（每交易日收盘后调用一次，用 bar 的 settle 价）。

        1) 持仓盯市盈亏（今昨分批按各自成本）全额计入 balance；
        2) 成本重置为今结算价，今仓毕业并入昨仓——防同一笔盈亏次日再计；
        3) margin_occupied 按结算价整体重置。
        缺结算价/缺乘数的持仓品种记入 errors 并跳过（持仓与该品种保证金
        占用保持原口径），绝不静默用 close 价冒充结算价。
        """
        position_pnl = 0.0
        errors: List[str] = []
        for pos in list(self._positions.values()):
            if pos.today_lots + pos.yesterday_lots == 0:
                continue
            settle_price = settle_prices.get(pos.symbol)
            multiplier = multipliers.get(pos.symbol)
            if settle_price is None or multiplier is None:
                if pos.symbol not in errors:
                    errors.append(pos.symbol)
                continue
            self._multipliers[pos.symbol] = multiplier
            sign = 1.0 if pos.direction == FuturesDirection.LONG else -1.0
            if pos.today_lots:
                position_pnl += (settle_price - pos.today_cost) * multiplier * pos.today_lots * sign
            if pos.yesterday_lots:
                position_pnl += (
                    (settle_price - pos.yesterday_cost) * multiplier * pos.yesterday_lots * sign
                )
            # 成本重置 + 今仓毕业：此后该批持仓的成本即今结算价
            pos.yesterday_cost = settle_price
            pos.yesterday_lots += pos.today_lots
            pos.today_lots = 0
            pos.today_cost = 0.0
        self.balance += position_pnl

        # 保证金重置：有结算价的按结算价；被跳过的品种保持原口径占用
        margin = 0.0
        for pos in self._positions.values():
            total_lots = pos.today_lots + pos.yesterday_lots
            if total_lots == 0:
                continue
            settle_price = settle_prices.get(pos.symbol)
            multiplier = multipliers.get(pos.symbol)
            if settle_price is not None and multiplier is not None:
                margin += settle_price * multiplier * total_lots * margin_rate
            else:
                margin += (
                    pos.yesterday_lots * pos.yesterday_cost + pos.today_lots * pos.today_cost
                ) * (multiplier or 0.0) * margin_rate
        self.margin_occupied = margin

        result = SettlementResult(
            trade_date=trade_date,
            position_pnl=position_pnl,
            balance_after=self.balance,
            margin_occupied_after=margin,
            errors=errors,
        )
        self.settlements.append(result)
        return result
