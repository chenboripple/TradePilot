"""
滚动前进（Walk-Forward）验证协议

背景：纯样本内网格搜索调参会系统性过拟合（参数朝着历史噪声拟合），
回测收益看起来很好、实盘大概率失效。本模块提供科学的前推验证：

- 把整段历史切成 ``n_splits`` 个连续片段；
- 每个片段内，前 ``train_ratio`` 为训练集（样本内，IS），其余为测试集
  （样本外，OOS）。训练集上对 ``param_grid`` 做网格搜索，只按
  “训练期夏普最高”选参；
- 用选出的参数在该片段的 OOS 区间跑 ``run_backtest``，
  训练与测试时间上严格隔离（OOS 段一定在训练段之后），策略实例由
  ``strategy_factory`` 每次新建，避免状态残留造成的信息泄漏；
- 拼接各分段的样本外指标，输出聚合 OOS 收益/平均夏普，以及
  ``overfit_gap``（平均 IS 收益 − 平均 OOS 收益，越大越可能过拟合）。

用法示例::

    from ripple_tradePilot.backtest.walkforward import walk_forward
    from ripple_tradePilot.strategies.moving_average import MovingAverageCross

    report = walk_forward(
        strategy_factory=lambda p: MovingAverageCross(fast=p["fast"], slow=p["slow"]),
        bars=bars,
        param_grid={"fast": [3, 5], "slow": [10, 20]},
        n_splits=3,
        train_ratio=0.7,
    )
    print(report.oos_total_return, report.overfit_gap)
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Callable, Dict, List, Optional, Sequence

from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import Metrics, compute_metrics
from ripple_tradePilot.models.types import Bar
from ripple_tradePilot.strategies.base import Strategy

# 训练/测试各自最少需要的 bar 数（compute_metrics 至少需要 2 个点）
_MIN_BARS_PER_SIDE = 2


@dataclass
class WalkForwardSplit:
    """单个滚动窗口的选参与样本外评估结果。

    区间均为左闭右开的 bars 下标：``[train_start, train_end)`` 为训练集，
    ``[test_start, test_end)`` 为测试集，且 ``train_end == test_start``，
    保证测试段严格位于训练段之后、无任何重叠。
    """

    split_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    best_params: Dict[str, object]   # 训练集网格搜索选出的参数
    is_metrics: Metrics              # 训练集（样本内）用最佳参数回测的指标
    oos_metrics: Metrics             # 测试集（样本外）用最佳参数回测的指标


@dataclass
class WalkForwardReport:
    """滚动前进验证的聚合报告。"""

    splits: List[WalkForwardSplit]
    oos_total_return: float   # 各 OOS 分段收益复利拼接后的总收益
    avg_is_return: float      # 平均样本内总收益
    avg_oos_return: float     # 平均样本外总收益
    avg_oos_sharpe: float     # 平均样本外夏普
    overfit_gap: float        # 平均 IS 收益 − 平均 OOS 收益，越大越可能过拟合


def _segment_edges(n_bars: int, n_splits: int) -> List[int]:
    """把 n_bars 根 bar 均分为 n_splits 段的边界下标（含首尾）。"""
    return [round(i * n_bars / n_splits) for i in range(n_splits + 1)]


def walk_forward(
    strategy_factory: Callable[[dict], Strategy],
    bars: Sequence[Bar],
    param_grid: Dict[str, List],
    n_splits: int = 3,
    train_ratio: float = 0.7,
    backtest_kwargs: Optional[dict] = None,
) -> WalkForwardReport:
    """滚动前进验证：训练段网格搜索选参，测试段样本外评估。

    Args:
        strategy_factory: 参数到策略实例的工厂函数。每次评估都会调用它
            新建实例，确保不同窗口/参数之间没有状态残留。
        bars: 按时间升序的 bar 序列（整段历史）。
        param_grid: 参数网格，形如 ``{"fast": [3, 5], "slow": [10, 20]}``，
            值取各列表的笛卡尔积。
        n_splits: 滚动窗口数量（把 bars 均分成几段）。
        train_ratio: 每段内训练集占比，取值 (0, 1)。
        backtest_kwargs: 透传给 ``run_backtest`` 的额外参数
            （如 ``initial_cash``、``execution``、``slippage`` 等）。

    Returns:
        WalkForwardReport：每个 split 的最佳参数与 IS/OOS 指标，
        以及聚合的 OOS 总收益、平均 OOS 夏普和过拟合缺口。

    Raises:
        ValueError: 窗口数/训练比例非法、参数网格为空，或数据量不足以
            支撑所要求的切分（每段的训练与测试都至少需要 2 根 bar）。
    """
    if n_splits < 1:
        raise ValueError("n_splits 必须为正整数")
    if not 0.0 < train_ratio < 1.0:
        raise ValueError("train_ratio 必须在 (0, 1) 开区间内")
    if not param_grid or any(len(list(values)) == 0 for values in param_grid.values()):
        raise ValueError("param_grid 不能为空")

    bar_list = list(bars)
    edges = _segment_edges(len(bar_list), n_splits)

    # 预先校验每一段的最小数据量，避免跑出无意义的指标
    for i in range(n_splits):
        size = edges[i + 1] - edges[i]
        train_len = int(round(size * train_ratio))
        if train_len < _MIN_BARS_PER_SIDE or size - train_len < _MIN_BARS_PER_SIDE:
            raise ValueError(
                f"数据量不足：第 {i} 段仅 {size} 根 bar，"
                f"无法同时满足训练/测试各至少 {_MIN_BARS_PER_SIDE} 根"
            )

    kwargs = dict(backtest_kwargs or {})
    param_names = list(param_grid.keys())
    combinations = [
        dict(zip(param_names, values))
        for values in product(*(param_grid[name] for name in param_names))
    ]

    splits: List[WalkForwardSplit] = []
    for i in range(n_splits):
        seg_start, seg_end = edges[i], edges[i + 1]
        train_end = seg_start + int(round((seg_end - seg_start) * train_ratio))
        train_bars = bar_list[seg_start:train_end]
        test_bars = bar_list[train_end:seg_end]

        # ---- 训练集网格搜索：按训练期夏普最高选参 ----
        best_params: Optional[dict] = None
        best_sharpe: Optional[float] = None
        best_metrics: Optional[Metrics] = None
        for params in combinations:
            strategy = strategy_factory(dict(params))
            result = run_backtest(strategy, train_bars, **kwargs)
            metrics = compute_metrics(result.equity_curve)
            # 夏普相同（如全为 0）时保留先出现的组合，保证结果确定可复现
            if best_sharpe is None or metrics.sharpe > best_sharpe:
                best_params, best_sharpe, best_metrics = params, metrics.sharpe, metrics

        # ---- 测试集样本外评估：同一参数、全新实例 ----
        oos_strategy = strategy_factory(dict(best_params))
        oos_result = run_backtest(oos_strategy, test_bars, **kwargs)
        oos_metrics = compute_metrics(oos_result.equity_curve)

        splits.append(
            WalkForwardSplit(
                split_index=i,
                train_start=seg_start,
                train_end=train_end,
                test_start=train_end,
                test_end=seg_end,
                best_params=dict(best_params),
                is_metrics=best_metrics,
                oos_metrics=oos_metrics,
            )
        )

    # ---- 聚合：OOS 收益复利拼接、IS/OOS 平均、过拟合缺口 ----
    is_returns = [s.is_metrics.total_return for s in splits]
    oos_returns = [s.oos_metrics.total_return for s in splits]
    compound = 1.0
    for ret in oos_returns:
        compound *= 1.0 + ret
    avg_is_return = sum(is_returns) / len(is_returns)
    avg_oos_return = sum(oos_returns) / len(oos_returns)

    return WalkForwardReport(
        splits=splits,
        oos_total_return=compound - 1.0,
        avg_is_return=avg_is_return,
        avg_oos_return=avg_oos_return,
        avg_oos_sharpe=sum(s.oos_metrics.sharpe for s in splits) / len(splits),
        overfit_gap=avg_is_return - avg_oos_return,
    )
