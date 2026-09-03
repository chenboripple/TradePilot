"""离线回测示例：读取本地 CSV 行情，用统一回测引擎跑均线策略并打印指标。

运行：PYTHONPATH=src python3 examples/run_backtest.py
"""

from pathlib import Path

from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import compute_metrics, compute_trade_stats
from ripple_tradePilot.data.loader import load_csv
from ripple_tradePilot.strategies.moving_average import MovingAverageCross

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "002022.SZ.csv"

bars = list(load_csv(DATA_FILE))
strategy = MovingAverageCross(fast=5, slow=20)
result = run_backtest(strategy, bars)

metrics = compute_metrics(result.equity_curve)
stats = compute_trade_stats(result.fills)

print(f"标的文件: {DATA_FILE.name}，{len(bars)} 根 bar"
      f"（{bars[0].timestamp.date()} ~ {bars[-1].timestamp.date()}）")
print(f"策略: {strategy.name} (fast={strategy.fast}, slow={strategy.slow})")
print("-" * 40)
print(f"总收益率:   {metrics.total_return:.2%}")
print(f"年化收益率: {metrics.annual_return:.2%}")
print(f"最大回撤:   {metrics.max_drawdown:.2%}")
print(f"夏普比率:   {metrics.sharpe:.2f}")
print("-" * 40)
print(f"交易回合数: {stats.num_trades}    胜率: {stats.win_rate:.2%}")
print(f"单笔最佳:   {stats.best_trade:.2%}    单笔最差: {stats.worst_trade:.2%}")
print(f"总手续费:   {stats.total_fees:.2f} 元")
print(f"期末权益:   {result.equity_curve[-1]:,.2f} 元")
if result.halted_by_drawdown:
    print("注意: 回测期间触发了回撤闸门，停止开新仓")
if result.skipped_fills:
    print(f"注意: {len(result.skipped_fills)} 笔信号因涨跌停无法成交")
