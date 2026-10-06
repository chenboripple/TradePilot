"""期货滚动样本外验证（roadmap P2 最后一条）。

「开展滚动样本外验证，保留最终未参与调参的数据，记录数据、参数和策略版本。」

结构（锚定式扩展训练 + 等分尾部测试块 + 独立保留集）：

- 日期轴尾部 ``holdout_ratio`` 比例为**保留集**——任何训练、选参与测试块都
  不触碰；保留集只在最后用「全量训练重选出的参数」跑一次。
- 保留集之前的日期切成 ``test_blocks`` 个等分测试块；第 i 块的训练集 = 该块
  之前的全部日期（扩展窗口，参数每块重选）。
- 每段独立账户（initial_cash 相同），指标只取段内 final_equity；OOS 总收益
  按各测试块收益**复利拼接**。
- 信号评估带预热前缀（指标连续性），但预热段不产生订单——交易只发生在
  测试块内，避免「分段冷启动让慢参数永远不出信号」的选参偏置。

选参口径：训练段 final_equity 最大者胜；平手取更小窗口（简约优先，防止
「参数越大越不容易亏」的退化偏好）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..data.futures_meta import RULE_VERSION
from ..signals.futures_eval import STRATEGY_KEY, DonchianParams, tilt_series
from .futures_engine import (
    ContractInput,
    EngineConfig,
    TiltSignal,
    run_futures_backtest,
)
from .futures_rules import FeeSchedule, MarginSchedule

__all__ = [
    "FuturesWalkforwardReport",
    "SegmentResult",
    "WalkforwardParams",
    "futures_walkforward",
    "main_timeline_signals",
]


@dataclass(frozen=True)
class WalkforwardParams:
    entry_windows: Tuple[int, ...] = (10, 20, 30)  # Donchian 突破窗候选（调参空间）
    test_blocks: int = 3        # OOS 测试块数
    holdout_ratio: float = 0.2  # 尾部保留集比例（永不参与选参）


@dataclass
class SegmentResult:
    index: int                       # 0..test_blocks-1；-1 = 保留集
    kind: str                        # 'test' | 'holdout'
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    chosen_entry_window: int
    train_equity: float              # 训练段（选中参数口径）期末权益
    test_equity: float
    test_return: float               # test_equity / initial_cash − 1
    fills: int = 0
    rolls: int = 0                   # 段内换月次数（主力切换平旧开新）


@dataclass
class FuturesWalkforwardReport:
    initial_cash: float
    segments: List[SegmentResult] = field(default_factory=list)
    manifest: Dict[str, object] = field(default_factory=dict)
    summary: Dict[str, object] = field(default_factory=dict)

    @property
    def holdout(self) -> Optional[SegmentResult]:
        found = [seg for seg in self.segments if seg.kind == "holdout"]
        return found[0] if found else None

    @property
    def oos_total_return(self) -> float:
        compounded = 1.0
        for seg in self.segments:
            if seg.kind == "test":
                compounded *= 1.0 + seg.test_return
        return compounded - 1.0


def main_timeline_signals(
    bars: List[dict],
    entry_window: int,
    *,
    symbol: str,
    main_dates: Optional[Sequence[str]] = None,
    atr_period: int = 14,
) -> List[TiltSignal]:
    """单合约时间线上的主力段倾向事件（换月期信号只认当时的主力合约）。

    main_dates=None → 全程主力（单合约回测）。评估窗口始终用该合约自身的
    完整历史（新主力在成为主力前已有真实成交，不造拼接序列）。
    """
    params = DonchianParams(entry_window=entry_window, atr_period=atr_period)
    main_set = set(main_dates) if main_dates is not None else None
    signals: List[TiltSignal] = []
    last: Optional[str] = None
    # tilt_series 一次算完每个前缀的倾向（旧逐前缀重算是 O(n²)，被网格放大）
    for index, tilt in enumerate(tilt_series(bars, params, symbol=symbol)):
        if main_set is not None and str(bars[index].get("trade_date", "")) not in main_set:
            continue  # 非主力日：不评估也不发信号（状态机到主力切换日自然重评）
        if tilt is None:
            continue
        if tilt.tilt != last:
            signals.append(TiltSignal(symbol=symbol, bar_index=index,
                                      direction=tilt.tilt))
            last = tilt.tilt
    return signals


def _trim(bars: Sequence[dict], start: str, end: str) -> List[dict]:
    return [bar for bar in bars
            if start <= str(bar.get("trade_date", "")) <= end]


def _warmup_prefix(bars: Sequence[dict], start: str, warmup: int) -> List[dict]:
    """start 之前最近 warmup 根 bar（指标连续性预热，段内不交易）。"""
    before = [bar for bar in bars if str(bar.get("trade_date", "")) < start]
    return before[-warmup:] if warmup else []


def futures_walkforward(
    contracts: Dict[str, ContractInput],
    config: EngineConfig,
    fee_schedule: FeeSchedule,
    margin_schedule: MarginSchedule,
    roll_schedule: Optional[Sequence[Tuple[str, str]]] = None,
    params: Optional[WalkforwardParams] = None,
    strategy_version: str = "",
) -> FuturesWalkforwardReport:
    """滚动选参 + 样本外测试 + 保留集验证。

    contracts 为同一品种的全部候选合约（换月交给引擎）；日期轴取全部合约
    60m bar 的交易日并集。每段独立账户、独立信号（预热不交易）。
    """
    if params is None:
        params = WalkforwardParams()
    if params.test_blocks < 1:
        raise ValueError("test_blocks 必须 ≥ 1")
    if not 0 < params.holdout_ratio < 1:
        raise ValueError("holdout_ratio 必须在 (0, 1)")
    bars_by_symbol = {
        symbol: sorted(data.bars_60m,
                       key=lambda row: (str(row.get("trade_date", "")),
                                        str(row.get("bar_time", ""))))
        for symbol, data in contracts.items()
    }
    all_dates = sorted({
        str(bar.get("trade_date", ""))
        for rows in bars_by_symbol.values() for bar in rows
        if str(bar.get("trade_date", ""))
    })
    if len(all_dates) < params.test_blocks * 4:
        raise ValueError(f"交易日不足（{len(all_dates)} 天），无法切 {params.test_blocks} 个测试块")

    warmup = max(params.entry_windows) + 15  # entry 窗 + ATR 周期余量
    main_by_date = dict(roll_schedule or [])

    def segment_dates() -> List[Tuple[str, str, str, str]]:
        """(train_start, train_end, test_start, test_end) 列表 + 末尾保留集。"""
        holdout_index = int(len(all_dates) * (1 - params.holdout_ratio))
        holdout_index = max(holdout_index, params.test_blocks * 2)
        pre_dates, holdout_dates = all_dates[:holdout_index], all_dates[holdout_index:]
        block_size = len(pre_dates) // (params.test_blocks + 1)
        segments = []
        for i in range(params.test_blocks):
            test_start_index = block_size * (i + 1)
            test_end_index = (block_size * (i + 2) if i < params.test_blocks - 1
                              else len(pre_dates))
            segments.append((
                pre_dates[0],
                pre_dates[test_start_index - 1],
                pre_dates[test_start_index],
                pre_dates[test_end_index - 1],
            ))
        segments.append((pre_dates[0], pre_dates[-1],
                         holdout_dates[0], holdout_dates[-1]))
        return segments

    def run_segment(train_start: str, train_end: str,
                    test_start: str, test_end: str) -> Tuple[int, float, float, int, int]:
        """训练段选参 → 测试段用选中参数独立跑一遍。"""
        chosen, train_equity = _select_window(
            bars_by_symbol, main_by_date, config, fee_schedule, margin_schedule,
            train_start, train_end, params.entry_windows, warmup, contracts,
            roll_schedule)
        report = _run_once(bars_by_symbol, main_by_date, config, fee_schedule,
                           margin_schedule, chosen, warmup, test_start, test_end,
                           contracts, roll_schedule)
        return chosen, train_equity, report.metrics["final_equity"], \
            int(report.metrics["fills"]), int(report.metrics["rolls"])

    def _select_window(bars_by_symbol, main_by_date, config, fee_schedule,
                       margin_schedule, start, end, windows, warmup, contracts,
                       roll_schedule):
        best_window, best_equity = None, None
        for window in sorted(windows):  # 升序遍历：平手时先到者（更小窗口）胜出
            report = _run_once(bars_by_symbol, main_by_date, config, fee_schedule,
                               margin_schedule, window, warmup, start, end,
                               contracts, roll_schedule)
            equity = report.metrics["final_equity"]
            if best_equity is None or equity > best_equity:
                best_window, best_equity = window, equity
        return best_window, best_equity

    def _run_once(bars_by_symbol, main_by_date, config, fee_schedule,
                  margin_schedule, entry_window, warmup, start, end, contracts,
                  roll_schedule):
        trimmed: Dict[str, ContractInput] = {}
        signals: List[TiltSignal] = []
        for symbol, data in contracts.items():
            bars = bars_by_symbol[symbol]
            prefix = _warmup_prefix(bars, start, warmup)
            body = _trim(bars, start, end)
            if not body:
                continue  # 该合约在段内无 bar（如尚未上市/已到期）
            segment_bars = prefix + body
            trimmed[symbol] = ContractInput(
                symbol=symbol, multiplier=data.multiplier, tick_size=data.tick_size,
                bars_60m=segment_bars, daily=data.daily)
            main_dates = [
                str(bar.get("trade_date", "")) for bar in segment_bars
                if str(bar.get("trade_date", "")) >= start
                and (not main_by_date or main_by_date.get(
                    str(bar.get("trade_date", "")), symbol) == symbol)
            ]
            signals.extend(main_timeline_signals(
                segment_bars, entry_window, symbol=symbol, main_dates=main_dates))
        if not trimmed:
            raise ValueError(f"段 [{start}, {end}] 无任何合约数据")
        return run_futures_backtest(
            trimmed, config, fee_schedule, margin_schedule, signals, roll_schedule)

    report = FuturesWalkforwardReport(initial_cash=config.initial_cash)
    raw_segments = segment_dates()
    for index, (train_start, train_end, test_start, test_end) in enumerate(raw_segments):
        kind = "holdout" if index == len(raw_segments) - 1 else "test"
        seg_index = -1 if kind == "holdout" else index
        chosen, train_equity, test_equity, fills, rolls = run_segment(
            train_start, train_end, test_start, test_end)
        report.segments.append(SegmentResult(
            index=seg_index, kind=kind,
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
            chosen_entry_window=chosen, train_equity=train_equity,
            test_equity=test_equity,
            test_return=test_equity / config.initial_cash - 1
            if config.initial_cash else 0.0,
            fills=fills, rolls=rolls))

    windows_used = [seg.chosen_entry_window for seg in report.segments]
    report.manifest = {
        "strategy_key": STRATEGY_KEY,
        # 参数族版本：网格本身属于「参数与策略版本」记录的一部分（验收要求）
        "strategy_version": strategy_version or (
            f"donchian-grid{sorted(params.entry_windows)}@{RULE_VERSION}"),
        "entry_windows": list(params.entry_windows),
        "test_blocks": params.test_blocks,
        "holdout_ratio": params.holdout_ratio,
        "data_start": all_dates[0], "data_end": all_dates[-1],
        "holdout_start": report.holdout.test_start if report.holdout else "",
        "fee_approximate": any(
            fee_schedule.is_approximate_on(date) for date in all_dates),
        "margin_approximate": any(
            margin_schedule.is_approximate_on(date) for date in all_dates),
        "symbols": sorted(contracts),
        "warmup_bars": warmup,
        "initial_cash": config.initial_cash,
    }
    report.summary = {
        "oos_total_return": report.oos_total_return,
        "holdout_return": report.holdout.test_return if report.holdout else None,
        "chosen_windows": windows_used,
        "test_fills_total": sum(seg.fills for seg in report.segments
                                if seg.kind == "test"),
        "test_rolls_total": sum(seg.rolls for seg in report.segments
                                if seg.kind == "test"),
    }
    return report
