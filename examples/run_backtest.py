from ripple_tradePilot.data.loader import load_csv
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import compute_metrics

bars = load_csv("data/sample_ohlcv.csv")
strategy = MovingAverageCross(fast=5, slow=20)
result = run_backtest(strategy, bars)
metrics = compute_metrics(result.equity_curve)

print(metrics)
