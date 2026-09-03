"""
通用技术指标计算（纯函数，基于 Python list[float]）

dashboard、监控与回测共用同一套指标实现，避免各处手写重复。
约定：序列前部数据不足以计算的位置以 None 占位。
"""

from __future__ import annotations

from math import sqrt
from typing import List, Optional, Sequence, Tuple

# combo_vote 类策略的默认投票阈值（dashboard 与监控共用）
DEFAULT_VOTE_THRESHOLD = 2


def rolling_mean(values: Sequence[float], window: int) -> List[Optional[float]]:
    """滚动均线：前 window-1 个位置为 None，之后为窗口内均值。"""
    result: List[Optional[float]] = [None] * len(values)
    running_total = 0.0
    for index, value in enumerate(values):
        running_total += value
        if index >= window:
            running_total -= values[index - window]
        if index >= window - 1:
            result[index] = running_total / window
    return result


def rsi_series(values: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """RSI 序列：简单窗口平均口径（与 dashboard 旧实现一致），前 period 个位置为 None。"""
    result: List[Optional[float]] = [None] * len(values)
    for index in range(period, len(values)):
        changes = [
            values[position] - values[position - 1]
            for position in range(index - period + 1, index + 1)
        ]
        gains = sum(max(change, 0) for change in changes) / period
        losses = sum(max(-change, 0) for change in changes) / period
        result[index] = 100.0 if losses == 0 else 100 - (100 / (1 + gains / losses))
    return result


def bollinger(
    values: Sequence[float],
    window: int = 20,
    num_std: float = 2.0,
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    """布林带：返回 (中轨, 上轨, 下轨)，前 window-1 个位置为 None；标准差按总体口径计算。"""
    middle: List[Optional[float]] = [None] * len(values)
    upper: List[Optional[float]] = [None] * len(values)
    lower: List[Optional[float]] = [None] * len(values)
    for index in range(window - 1, len(values)):
        window_values = values[index - window + 1:index + 1]
        average = sum(window_values) / window
        deviation = sqrt(sum((value - average) ** 2 for value in window_values) / window)
        middle[index] = average
        upper[index] = average + num_std * deviation
        lower[index] = average - num_std * deviation
    return middle, upper, lower
