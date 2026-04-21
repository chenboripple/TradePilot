from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np


@dataclass
class Metrics:
    total_return: float
    max_drawdown: float
    sharpe: float


def compute_metrics(equity_curve: List[float]) -> Metrics:
    eq = np.array(equity_curve, dtype=float)
    returns = np.diff(eq) / eq[:-1]
    total_return = eq[-1] / eq[0] - 1
    peak = np.maximum.accumulate(eq)
    drawdown = (eq - peak) / peak
    max_drawdown = drawdown.min() if len(drawdown) else 0.0
    sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0.0
    return Metrics(total_return=total_return, max_drawdown=max_drawdown, sharpe=sharpe)
