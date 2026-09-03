"""
统一回测引擎回归测试

用固定夹具锁定撮合语义，防止未来改动悄悄引入偏差：
- 因果性：信号在第 i 根 bar 产生，默认第 i+1 根开盘成交
- 涨跌停约束、100 股整数倍、佣金/印花税
- 权益曲线逐 bar 用真实仓位计算（旧引擎此处恒为 0 回撤）
- 现金账目在部分仓位下不被清零（旧引擎覆盖写 bug 回归）
"""

import unittest
from datetime import datetime, timedelta
from typing import List, Optional

from ripple_tradePilot.backtest.engine import LOT_SIZE, run_backtest
from ripple_tradePilot.backtest.report import compute_metrics, compute_trade_stats, pair_trades
from ripple_tradePilot.models.types import Bar, Side, Signal
from ripple_tradePilot.risk.manager import RiskConfig
from ripple_tradePilot.strategies.base import Strategy


def make_bars(closes, opens=None) -> List[Bar]:
    """按收盘价序列生成 OHLC 夹具（开高低围绕收盘价构造）。"""
    base = datetime(2026, 1, 5)
    bars = []
    for index, close in enumerate(closes):
        open_price = opens[index] if opens else close
        bars.append(
            Bar(
                timestamp=base + timedelta(days=index),
                open=open_price,
                high=max(open_price, close) * 1.001,
                low=min(open_price, close) * 0.999,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


class ScriptedStrategy(Strategy):
    """按脚本逐 bar 发信号的测试策略。"""

    name = "scripted"

    def __init__(self, actions: List[Optional[Side]]):
        self.actions = actions
        self.index = 0
        self.bars_seen: List[Bar] = []

    def on_bar(self, bar: Bar) -> Signal:
        self.bars_seen.append(bar)
        side = self.actions[self.index] if self.index < len(self.actions) else None
        self.index += 1
        return Signal(timestamp=bar.timestamp, side=side)


NO_RISK = RiskConfig(
    max_position_pct=1.0,
    stop_loss_pct=0.99,
    take_profit_pct=9.99,
    max_drawdown_pct=0.99,
)


class EngineExecutionTest(unittest.TestCase):
    def test_next_open_execution_is_causal(self):
        """第 0 根 bar 的 BUY 信号，必须在第 1 根开盘价成交，而非当根收盘价。"""
        bars = make_bars([10.0, 10.5, 10.9])
        strategy = ScriptedStrategy([Side.BUY, None, None, None])

        result = run_backtest(
            strategy, bars, execution="next_open", slippage=0.0, risk_config=NO_RISK
        )

        self.assertEqual(len(result.fills), 1)
        fill = result.fills[0]
        self.assertEqual(fill.side, Side.BUY)
        self.assertEqual(fill.price, 10.5)  # 第 1 根开盘价，不是信号 bar 的收盘 10.0
        self.assertEqual(fill.timestamp, bars[1].timestamp)

    def test_next_open_uses_open_not_close(self):
        bars = make_bars([10.0, 11.0, 12.0], opens=[10.0, 10.2, 11.5])
        strategy = ScriptedStrategy([Side.BUY, None, None, None])

        result = run_backtest(strategy, bars, execution="next_open", slippage=0.0)

        self.assertEqual(result.fills[0].price, 10.2)

    def test_close_mode_fills_same_bar(self):
        bars = make_bars([10.0, 11.0, 12.0])
        strategy = ScriptedStrategy([None, Side.BUY, None, None])

        result = run_backtest(
            strategy, bars, execution="close", slippage=0.0, risk_config=NO_RISK
        )

        self.assertEqual(result.fills[0].price, 11.0)
        self.assertEqual(result.fills[0].timestamp, bars[1].timestamp)

    def test_quantity_is_round_lot(self):
        bars = make_bars([10.0, 10.5, 10.9])
        strategy = ScriptedStrategy([Side.BUY, None, None, None])

        result = run_backtest(
            strategy,
            bars,
            initial_cash=10_000,
            execution="next_open",
            slippage=0.0,
            risk_config=NO_RISK,
        )

        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].quantity % LOT_SIZE, 0)
        self.assertGreater(result.fills[0].quantity, 0)
        # 买入后现金不为负
        spent = result.fills[0].quantity * result.fills[0].price + result.fills[0].fee
        self.assertLessEqual(spent, 10_000)
        self.assertAlmostEqual(result.fills[0].price, 10.5)

    def test_limit_up_blocks_buy(self):
        # 前收 10.0，开盘 11.0 = 涨停 → 买不进
        bars = make_bars([10.0, 11.0, 12.0], opens=[10.0, 11.0, 11.5])
        strategy = ScriptedStrategy([Side.BUY, None, None, None])

        result = run_backtest(strategy, bars, execution="next_open", slippage=0.0)

        self.assertEqual(result.fills, [])
        self.assertEqual(len(result.skipped_fills), 1)
        self.assertIn("涨停", result.skipped_fills[0]["reason"])

    def test_limit_down_blocks_sell(self):
        risk = RiskConfig(
            max_position_pct=1.0, stop_loss_pct=0.99, take_profit_pct=9.99, max_drawdown_pct=0.99
        )
        # bar1 开盘买入 @10；bar2 信号卖出，但 bar3 开盘 9.0 相对前收 10.0 跌停
        bars = make_bars([10.0, 10.0, 10.0, 9.5], opens=[10.0, 10.0, 10.0, 9.0])
        strategy = ScriptedStrategy([Side.BUY, None, Side.SELL, None])

        result = run_backtest(strategy, bars, execution="next_open", slippage=0.0, risk_config=risk)

        buys = [f for f in result.fills if f.side == Side.BUY]
        sells = [f for f in result.fills if f.side == Side.SELL]
        self.assertEqual(len(buys), 1)
        self.assertEqual(len(sells), 0)  # 跌停卖不出
        self.assertTrue(any("跌停" in s["reason"] for s in result.skipped_fills))

    def test_fees_include_commission_and_stamp_duty(self):
        bars = make_bars([10.0] * 6, opens=[10.0] * 6)
        strategy = ScriptedStrategy([Side.BUY, None, None, Side.SELL, None, None])

        result = run_backtest(
            strategy,
            bars,
            initial_cash=100_000,
            fee_rate=0.001,
            stamp_duty=0.001,
            slippage=0.0,
            execution="next_open",
        )

        buy, sell = result.fills[0], result.fills[1]
        self.assertAlmostEqual(buy.fee, buy.quantity * 10.0 * 0.001, places=6)
        expected_sell_fee = (
            sell.quantity * 10.0 * 0.001 + sell.quantity * 10.0 * 0.001
        )  # 佣金 + 印花税
        self.assertAlmostEqual(sell.fee, expected_sell_fee, places=6)
        # 平价进出，净亏损 = 全部费用
        final_equity = result.equity_curve[-1]
        self.assertAlmostEqual(
            final_equity, 100_000 - buy.fee - sell.fee, places=4
        )

    def test_cash_preserved_with_partial_position(self):
        """回归：旧引擎 `cash = proceeds - fee` 覆盖写导致部分仓位下现金清零。"""
        risk = RiskConfig(
            max_position_pct=0.5, stop_loss_pct=0.99, take_profit_pct=9.99, max_drawdown_pct=0.99
        )
        bars = make_bars([10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
        strategy = ScriptedStrategy(
            [Side.BUY, None, None, Side.SELL, None, Side.BUY, None, None]
        )

        result = run_backtest(
            strategy,
            bars,
            initial_cash=100_000,
            fee_rate=0.0003,
            slippage=0.0,
            execution="next_open",
            risk_config=risk,
        )

        buys = [f for f in result.fills if f.side == Side.BUY]
        # 第一回合只用了 50% 资金；卖出后剩余现金必须仍在账上，第二轮才买得进
        self.assertEqual(len(buys), 2)
        self.assertGreater(result.equity_curve[-1], 90_000)


class EngineEquityTest(unittest.TestCase):
    def test_equity_curve_marks_position_to_market(self):
        bars = make_bars([10.0, 10.0, 10.8, 11.5])
        strategy = ScriptedStrategy([Side.BUY, None, None, None])

        result = run_backtest(
            strategy, bars, execution="next_open", slippage=0.0, risk_config=NO_RISK
        )

        quantity = result.fills[0].quantity
        cash = 100_000 - quantity * 10.0 - result.fills[0].fee
        self.assertAlmostEqual(result.equity_curve[0], 100_000)  # 未成交
        self.assertAlmostEqual(result.equity_curve[1], cash + quantity * 10.0)
        self.assertAlmostEqual(result.equity_curve[2], cash + quantity * 10.8)
        self.assertAlmostEqual(result.equity_curve[3], cash + quantity * 11.5)

    def test_drawdown_halt_stops_new_buys_but_keeps_timeline(self):
        risk = RiskConfig(
            max_position_pct=1.0, stop_loss_pct=0.99, take_profit_pct=9.99, max_drawdown_pct=0.05
        )
        bars = make_bars([10.0, 10.0, 8.0, 6.0, 8.0, 10.0])
        strategy = ScriptedStrategy([Side.BUY, None, Side.SELL, None, Side.BUY, None])

        result = run_backtest(strategy, bars, execution="next_open", slippage=0.0, risk_config=risk)

        self.assertTrue(result.halted_by_drawdown)
        # 时间线完整：权益曲线不因熔断截断
        self.assertEqual(len(result.equity_curve), len(bars))
        # 回撤闸门后不允许再开新仓
        buys = [f for f in result.fills if f.side == Side.BUY]
        self.assertEqual(len(buys), 1)

    def test_drawdown_and_sharpe_are_real(self):
        """旧引擎在下跌行情回撤恒为 0；此处固定正确值量级。"""
        bars = make_bars([10.0, 10.0, 10.0, 7.0, 7.0, 7.0, 7.0, 7.0])
        strategy = ScriptedStrategy([Side.BUY, None, None, None, None, None, None, None])

        result = run_backtest(strategy, bars, execution="next_open", slippage=0.0)

        metrics = compute_metrics(result.equity_curve)
        self.assertLess(metrics.max_drawdown, -0.20)  # 10 → 7 约 -30%
        self.assertLess(metrics.total_return, -0.20)


class TradeStatsTest(unittest.TestCase):
    def test_pair_and_stats(self):
        bars = make_bars([10.0, 10.0, 12.0, 12.0, 12.0])
        strategy = ScriptedStrategy([Side.BUY, None, Side.SELL, None, None])

        result = run_backtest(strategy, bars, execution="next_open", slippage=0.0)

        trades = pair_trades(result.fills)
        self.assertEqual(len(trades), 1)
        self.assertGreater(trades[0]["return"], 0.15)  # 10 → 12

        stats = compute_trade_stats(result.fills)
        self.assertEqual(stats.num_trades, 1)
        self.assertEqual(stats.win_rate, 1.0)
        self.assertGreater(stats.total_fees, 0)


if __name__ == "__main__":
    unittest.main()
