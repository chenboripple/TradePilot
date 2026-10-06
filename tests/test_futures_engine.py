"""期货回测撮合引擎测试（roadmap P2 验收「固定案例逐笔核对」）。

每个用例的期望数字全部手算（RB：乘数 10、tick 1、滑点 1 跳、保证金率 0.13、
手续费 3.1 元/手），对不上就是引擎或账本错了——不许改数字迁就实现。

黄金总账不变式（贯穿所有用例）：结算盯市盈亏 + 平仓盈亏 + 费用 == 全程价差
× 乘数 × 手数（逐日盯市成本重置后**不重复计入**的证明）。
"""

import tempfile
import unittest
from pathlib import Path

from ripple_tradePilot.backtest.futures_engine import (
    ContractInput,
    EngineConfig,
    TiltSignal,
    build_roll_schedule,
    donchian_tilt_signals,
    run_futures_backtest,
)
from ripple_tradePilot.backtest.futures_rules import (
    FeeRule,
    FeeSchedule,
    MarginRule,
    MarginSchedule,
)
from ripple_tradePilot.storage.database import (
    insert_futures_orders,
    insert_futures_trades,
    load_futures_account_daily,
    load_futures_orders,
    load_futures_trades,
    upsert_futures_account_daily,
)

SYM = "RB2610.SHFE"
D1, D2, D3 = "20260921", "20260922", "20260923"

MULT, TICK = 10.0, 1.0


def bar(trade_date, bar_time, open_, close_, volume=100000, high=None, low=None):
    return {"trade_date": trade_date, "bar_time": bar_time, "open": open_,
            "high": high if high is not None else max(open_, close_),
            "low": low if low is not None else min(open_, close_),
            "close": close_, "volume": volume}


def daily(trade_date, settle, volume=100000, hold=50000):
    return {"trade_date": trade_date, "settle": settle, "volume": volume, "hold": hold}


def flat_fee_rules(open_=3.1, close_today=3.1, close_yesterday=3.1):
    return FeeSchedule([FeeRule(effective_from="20200101", effective_to=None,
                                open_per_lot=open_, close_today_per_lot=close_today,
                                close_yesterday_per_lot=close_yesterday)])


MARGINS = MarginSchedule([MarginRule("20200101", None, 0.13)])


def contract(bars, dailies, sym=SYM):
    return ContractInput(symbol=sym, multiplier=MULT, tick_size=TICK,
                         bars_60m=bars or [], daily=dailies or [])


def base_config(**overrides):
    settings = {"initial_cash": 200000.0, "slippage_ticks": 1,
                "max_volume_participation": 0.1, "run_id": "test-run"}
    settings.update(overrides)
    return EngineConfig(**settings)


def three_day_bars():
    """两日信号、三日数据的标准夹具：D1 尾 bar 出信号 → D2 首 bar 开仓成交。"""
    return [
        bar(D1, "2026-09-21 10:30:00", 3090, 3095),
        bar(D1, "2026-09-21 11:30:00", 3095, 3100),
        bar(D2, "2026-09-22 10:30:00", 3100, 3105),
        bar(D2, "2026-09-22 11:30:00", 3105, 3080),
        bar(D3, "2026-09-23 10:30:00", 3160, 3150),
        bar(D3, "2026-09-23 11:30:00", 3150, 3155),
    ]


def three_day_daily():
    return [daily(D1, 3098), daily(D2, 3120), daily(D3, 3150)]


class LongRoundTripTest(unittest.TestCase):
    """多头：D2 开仓@3101 → D2 结算@3120 盯市 +190 → D3 平昨@3159 +390。

    总账：200000 + 190 + 390 − 3.1×2 = 200573.8，恰等于全程价差
    (3159−3101)×10 − 费用（结算盈亏与平仓盈亏合计，无重复计入）。
    """

    def test_full_ledger(self):
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), three_day_daily())},
            base_config(), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "LONG"), TiltSignal(SYM, 3, "NEUTRAL")])
        trades = report.trades
        self.assertEqual(len(trades), 2)
        with self.subTest(step="开仓"):
            self.assertEqual(trades[0]["open_close"], "OPEN")
            self.assertEqual(trades[0]["price"], 3101)   # 3100 + 1 跳滑点
            self.assertEqual(trades[0]["fee"], 3.1)
            self.assertIsNone(trades[0]["realized_pnl"])
        with self.subTest(step="跨日平仓自动改道平昨"):
            # 信号在 D2 产生（今仓），成交在 D3（已毕业为昨仓）→ 改道 CLOSE_YESTERDAY
            self.assertEqual(trades[1]["open_close"], "CLOSE_YESTERDAY")
            self.assertEqual(trades[1]["price"], 3159)   # 3160 − 1 跳
            self.assertEqual(trades[1]["close_from_yesterday"], 1)
            self.assertAlmostEqual(trades[1]["realized_pnl"], 390.0)  # (3159−3120)×10
        with self.subTest(step="逐日快照"):
            self.assertEqual([row["trade_date"] for row in report.daily],
                             [D1, D2, D3])
            self.assertEqual(report.daily[0]["balance"], 200000.0)
            self.assertAlmostEqual(report.daily[1]["position_pnl_today"], 190.0)
            self.assertEqual(report.daily[2]["realized_pnl_today"], 390.0)
            self.assertAlmostEqual(report.daily[2]["fees_today"], 3.1)
        with self.subTest(step="黄金总账"):
            self.assertAlmostEqual(report.metrics["final_equity"], 200573.8)
            self.assertAlmostEqual(report.metrics["fees_total"], 6.2)
            self.assertEqual(report.metrics["fills"], 2)
            self.assertEqual(report.metrics["orders_by_status"].get("FILLED"), 2)
            self.assertEqual(report.metrics["max_drawdown"], 0.0)


class ShortRoundTripTest(unittest.TestCase):
    """空头镜像：D2 开空@3099 → D2 结算@3050 盯市 +490 → D3 平昨@3041 +90。"""

    def test_full_ledger(self):
        bars = three_day_bars()
        bars[4] = bar(D3, "2026-09-23 10:30:00", 3040, 3050)   # 平仓买回基价
        dailies = [daily(D1, 3098), daily(D2, 3050), daily(D3, 3040)]
        report = run_futures_backtest(
            {SYM: contract(bars, dailies)}, base_config(), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "SHORT"), TiltSignal(SYM, 3, "NEUTRAL")])
        trades = report.trades
        self.assertEqual(trades[0]["open_close"], "OPEN")
        self.assertEqual(trades[0]["direction"], "SHORT")
        self.assertEqual(trades[0]["price"], 3099)       # 3100 − 1 跳（卖出吃亏）
        self.assertAlmostEqual(report.daily[1]["position_pnl_today"], 490.0)
        self.assertEqual(trades[1]["price"], 3041)       # 3040 + 1 跳（买入吃亏）
        self.assertAlmostEqual(trades[1]["realized_pnl"], 90.0)
        # 全程价差 (3099−3041)×10 = 580，扣双边费用 6.2
        self.assertAlmostEqual(report.metrics["final_equity"], 200573.8)


class OffsetFeeTierTest(unittest.TestCase):
    """三档费用互不串档：平今走 close_today 档，跨日平昨走 close_yesterday 档。"""

    def test_same_day_close_pays_today_tier(self):
        bars = [
            bar(D1, "2026-09-21 10:30:00", 3090, 3095),
            bar(D1, "2026-09-21 11:30:00", 3095, 3100),
            bar(D2, "2026-09-22 10:30:00", 3100, 3105),
            bar(D2, "2026-09-22 11:30:00", 3120, 3125),
        ]
        report = run_futures_backtest(
            {SYM: contract(bars, [daily(D1, 3098), daily(D2, 3120)])},
            base_config(), flat_fee_rules(open_=3.1, close_today=4.1,
                                          close_yesterday=5.1),
            MARGINS, [TiltSignal(SYM, 1, "LONG"), TiltSignal(SYM, 2, "NEUTRAL")])
        self.assertEqual(report.trades[1]["open_close"], "CLOSE_TODAY")
        self.assertEqual(report.trades[1]["fee"], 4.1)
        self.assertEqual(report.trades[1]["close_from_today"], 1)

    def test_overnight_close_pays_yesterday_tier(self):
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), three_day_daily())},
            base_config(), flat_fee_rules(open_=3.1, close_today=4.1,
                                          close_yesterday=5.1),
            MARGINS, [TiltSignal(SYM, 1, "LONG"), TiltSignal(SYM, 3, "NEUTRAL")])
        self.assertEqual(report.trades[1]["open_close"], "CLOSE_YESTERDAY")
        self.assertEqual(report.trades[1]["fee"], 5.1)


class GateTest(unittest.TestCase):
    """无法成交路径：资金不足 / 规则缺失 / 涨跌停——全部拒单且不产生交易。"""

    def test_margin_shortfall_rejects_open(self):
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), three_day_daily())},
            base_config(initial_cash=4000.0), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "LONG")])
        self.assertEqual(report.trades, [])
        rejected = [o for o in report.orders if o.status.value == "REJECTED"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].reason, "margin_shortfall")
        self.assertAlmostEqual(report.metrics["final_equity"], 4000.0)

    def test_missing_margin_rule_rejects_open(self):
        margins = MarginSchedule([MarginRule("20200101", D1, 0.13)])  # D2 起缺规则
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), three_day_daily())},
            base_config(), flat_fee_rules(), margins, [TiltSignal(SYM, 1, "LONG")])
        rejected = [o for o in report.orders if o.status.value == "REJECTED"]
        self.assertEqual(rejected[0].reason, "margin_rule_missing")
        self.assertEqual(report.trades, [])

    def test_missing_fee_rule_rejects_open(self):
        fees = FeeSchedule([FeeRule(effective_from=D2, effective_to=None,
                                    close_yesterday_per_lot=3.1)])  # 开仓档缺失
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), three_day_daily())},
            base_config(), fees, MARGINS, [TiltSignal(SYM, 1, "LONG")])
        rejected = [o for o in report.orders if o.status.value == "REJECTED"]
        self.assertEqual(rejected[0].reason, "fee_missing")
        self.assertEqual(report.trades, [])

    def test_limit_up_blocks_buy(self):
        # D1 结算 2900 → D2 涨停 = round(2900×1.07) = 3103；买单 3103+1 跳 = 3104 ≥ 3103 拒
        bars = three_day_bars()
        bars[2] = bar(D2, "2026-09-22 10:30:00", 3103, 3105)
        report = run_futures_backtest(
            {SYM: contract(bars, [daily(D1, 2900), daily(D2, 3120), daily(D3, 3150)])},
            base_config(limit_pct=0.07), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "LONG")])
        rejected = [o for o in report.orders if o.status.value == "REJECTED"]
        self.assertEqual(rejected[0].reason, "limit_up")
        self.assertEqual(report.trades, [])

    def test_limit_down_blocks_sell(self):
        # D1 结算 3300 → D2 跌停 = round(3300×0.93) = 3069；卖单 3070−1 跳 = 3069 ≤ 3069 拒
        bars = three_day_bars()
        bars[2] = bar(D2, "2026-09-22 10:30:00", 3070, 3105)
        report = run_futures_backtest(
            {SYM: contract(bars, [daily(D1, 3300), daily(D2, 3120), daily(D3, 3150)])},
            base_config(limit_pct=0.07), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "SHORT")])
        rejected = [o for o in report.orders if o.status.value == "REJECTED"]
        self.assertEqual(rejected[0].reason, "limit_down")
        self.assertEqual(report.trades, [])


class VolumeCapTest(unittest.TestCase):
    """成交量约束：余量部分成交后当 bar 撤销；上限为 0 时整单拒绝。"""

    def _run(self, bar_volume):
        bars = [
            bar(D1, "2026-09-21 10:30:00", 3090, 3095, volume=bar_volume),
            bar(D1, "2026-09-21 11:30:00", 3095, 3100, volume=bar_volume),
            bar(D2, "2026-09-22 10:30:00", 3100, 3105, volume=bar_volume),
            bar(D2, "2026-09-22 11:30:00", 3110, 3115, volume=bar_volume),
            bar(D3, "2026-09-23 10:30:00", 3150, 3155, volume=bar_volume),
        ]
        return run_futures_backtest(
            {SYM: contract(bars, [daily(D1, 3098), daily(D2, 3120), daily(D3, 3130)])},
            base_config(lots_per_signal=2), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "LONG"), TiltSignal(SYM, 3, "NEUTRAL")])

    def test_partial_fill_then_cancel(self):
        # volume 15 × participation 0.1 → cap 1 手：2 手订单成 1 手、余 1 手撤销
        report = self._run(15)
        self.assertEqual(len(report.trades), 2)
        self.assertEqual(report.trades[0]["lots"], 1)
        partial = [o for o in report.orders if o.filled_lots == 1][0]
        self.assertEqual(partial.status.value, "CANCELLED")
        self.assertEqual(partial.reason, "volume_cap")
        # 持仓 1 手照常走完结算与平仓：费用手算 3.1×2
        self.assertAlmostEqual(report.metrics["fees_total"], 6.2)

    def test_zero_cap_rejects(self):
        report = self._run(5)   # cap = 0
        self.assertEqual(report.trades, [])
        rejected = [o for o in report.orders if o.status.value == "REJECTED"]
        self.assertEqual(rejected[0].reason, "volume_cap")


class SettlementGapTest(unittest.TestCase):
    """结算价缺失：当日不结算（不拿收盘价冒充），快照记 settlement_errors。"""

    def test_missing_settle_recorded_not_fabricated(self):
        dailies = [daily(D1, 3098), daily(D2, None), daily(D3, 3150)]
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), dailies)}, base_config(),
            flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "LONG"), TiltSignal(SYM, 3, "NEUTRAL")])
        row = report.daily[1]
        self.assertIn(SYM, row["settlement_errors"])
        self.assertEqual(row["balance"], 200000.0 - 3.1)  # 未结算：零盯市
        self.assertEqual(row["position_pnl_today"], 0.0)
        self.assertEqual(report.metrics["settlement_error_days"], 1)
        # D2 未结算 → D3 平仓时持仓仍是今仓口径（成本 = 开仓价），不重复计盈亏
        self.assertEqual(report.trades[1]["open_close"], "CLOSE_TODAY")
        self.assertAlmostEqual(report.trades[1]["realized_pnl"], (3159 - 3101) * MULT)


class RollTest(unittest.TestCase):
    """换月：主力切换日显式「平旧开新」，净方向保持，旧腿成本可核对。"""

    OLD, NEW = "RB2610.SHFE", "RB2701.SHFE"

    def _run(self):
        old_bars = [
            bar(D1, "2026-09-21 10:30:00", 3090, 3095),
            bar(D1, "2026-09-21 11:30:00", 3095, 3100),
            bar(D2, "2026-09-22 10:30:00", 3100, 3105),
            bar(D2, "2026-09-22 11:30:00", 3105, 3108),
            bar(D3, "2026-09-23 10:30:00", 3150, 3148),
        ]
        new_bars = [
            bar(D3, "2026-09-23 10:30:00", 3160, 3162),
            bar(D3, "2026-09-23 11:30:00", 3170, 3168),
        ]
        contracts = {
            self.OLD: contract(old_bars,
                               [daily(D1, 3098), daily(D2, 3120, volume=20000),
                                daily(D3, 3130)],
                               sym=self.OLD),
            self.NEW: contract(new_bars,
                               [daily(D1, 3155, volume=10000),
                                daily(D2, 3160, volume=60000),
                                daily(D3, 3165, volume=80000)],
                               sym=self.NEW),
        }
        old_daily = {self.OLD: contracts[self.OLD].daily,
                     self.NEW: contracts[self.NEW].daily}
        schedule = build_roll_schedule(old_daily)
        report = run_futures_backtest(
            contracts, base_config(), flat_fee_rules(), MARGINS,
            [TiltSignal(self.OLD, 1, "LONG")], schedule)
        return schedule, report

    def test_roll_schedule_no_lookahead(self):
        schedule, _ = self._run()
        # D3 主力由 D2 成交量决定（60k > 20k）；D2 主力由 D1 决定；首日自举
        self.assertEqual(schedule, [(D1, self.OLD), (D2, self.OLD), (D3, self.NEW)])

    def test_roll_closes_old_and_opens_new(self):
        _, report = self._run()
        self.assertEqual(report.rolls,
                         [{"trade_date": D3, "from": self.OLD, "to": self.NEW,
                           "net_lots": 1}])
        trades = report.trades
        with self.subTest(step="旧腿平昨"):
            close = [t for t in trades if t["symbol"] == self.OLD
                     and t["open_close"] != "OPEN"][0]
            self.assertTrue(close["is_roll"])
            self.assertEqual(close["price"], 3149)          # 3150 − 1 跳
            self.assertAlmostEqual(close["realized_pnl"], (3149 - 3120) * MULT)
        with self.subTest(step="新腿开多"):
            open_new = [t for t in trades if t["symbol"] == self.NEW][0]
            self.assertTrue(open_new["is_roll"])
            self.assertEqual(open_new["direction"], "LONG")
            self.assertEqual(open_new["price"], 3161)       # 3160 + 1 跳
        with self.subTest(step="数据结束强平新腿"):
            forced = [t for t in trades if t["is_forced"]][0]
            self.assertEqual(forced["price"], 3167)         # 最后一根收盘 3168 − 1 跳（卖出）
        # 总账：190(结算) + 290(旧腿) + 60(新腿 3167−3161) − 3.1×4（四笔成交各付费）
        self.assertAlmostEqual(report.metrics["final_equity"], 200527.6)
        self.assertEqual(report.metrics["rolls"], 1)


class ForcedLiquidationTest(unittest.TestCase):
    """保证金不足强平：结算后可用为负 → 下一日强平，受成交量约束逐笔留痕。"""

    def test_short_crash_triggers_forced_close(self):
        lots = 40
        bars = [
            bar(D1, "2026-09-21 10:30:00", 3090, 3095),
            bar(D1, "2026-09-21 11:30:00", 3095, 3100),
            bar(D2, "2026-09-22 10:30:00", 3100, 3105),
            bar(D2, "2026-09-22 11:30:00", 3105, 3108),
            bar(D3, "2026-09-23 10:30:00", 3950, 3960),
            bar(D3, "2026-09-23 11:30:00", 3955, 3958),
        ]
        dailies = [daily(D1, 3098), daily(D2, 3900), daily(D3, 3960)]
        report = run_futures_backtest(
            {SYM: contract(bars, dailies)},
            base_config(lots_per_signal=lots), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "SHORT")])
        # D2 结算：空头 (3099−3900)×10×40 = −320400 → 结存 −120524，触发强平
        self.assertAlmostEqual(report.daily[1]["balance"], 200000.0 - 124.0 - 320400.0)
        self.assertTrue(any("强平" in w for w in report.warnings))
        forced = [t for t in report.trades if t["is_forced"]]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["lots"], lots)
        self.assertEqual(forced[0]["price"], 3951)          # 3950 + 1 跳买回
        self.assertEqual(report.metrics["forced_liquidations"], 1)
        # 强平腿盈亏 (3900−3951)×10×40 = −51×400 = −20400
        self.assertAlmostEqual(forced[0]["realized_pnl"], -20400.0)
        self.assertAlmostEqual(
            report.metrics["final_equity"],
            200000.0 - 124.0 - 320400.0 - 124.0 - 20400.0)


class EndOfDataTest(unittest.TestCase):
    """信号在最后一根 bar 产生 → 无下一根 → 撤单 no_next_bar，不产生交易。"""

    def test_signal_on_last_bar_cancelled(self):
        bars = three_day_bars()
        report = run_futures_backtest(
            {SYM: contract(bars, three_day_daily())}, base_config(),
            flat_fee_rules(), MARGINS, [TiltSignal(SYM, 5, "LONG")])
        cancelled = [o for o in report.orders if o.status.value == "CANCELLED"]
        self.assertEqual(cancelled[0].reason, "no_next_bar")
        self.assertEqual(report.trades, [])


class DonchianAdapterTest(unittest.TestCase):
    """倾向 → 边沿事件：只保留方向变化，与 P1 evaluate_tilt 同口径。"""

    def test_emits_direction_changes_only(self):
        bars = []
        for i in range(20):
            bars.append({"bar_time": f"2026-09-21 {9 + i // 2:02d}:{30 * (i % 2):02d}:00",
                         "open": 100 + i, "high": 100 + i, "low": 99 + i,
                         "close": 100 + i})
        bars.append({"bar_time": "2026-09-21 20:00:00", "open": 120, "high": 121,
                     "low": 119, "close": 130})   # 上破 20 根高点 → LONG
        bars.append({"bar_time": "2026-09-21 21:00:00", "open": 128, "high": 131,
                     "low": 100, "close": 105})   # 回到通道内 → NEUTRAL
        bars.append({"bar_time": "2026-09-21 22:00:00", "open": 104, "high": 106,
                     "low": 103, "close": 105})   # 维持观望 → 无新事件
        signals = donchian_tilt_signals(SYM, bars)
        self.assertEqual([(s.bar_index, s.direction) for s in signals],
                         [(20, "LONG"), (21, "NEUTRAL")])


class AuditStorageRoundtripTest(unittest.TestCase):
    """报告 → v17 审计三表落库 → 按 run_id 完整重放（验收「能重现报告」）。"""

    def test_orders_trades_daily_roundtrip(self):
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), three_day_daily())},
            base_config(), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "LONG"), TiltSignal(SYM, 3, "NEUTRAL")])
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "engine.db"
            insert_futures_orders(report.order_rows("rt-1"), target)
            insert_futures_trades(report.trade_rows("rt-1"), target)
            upsert_futures_account_daily(report.daily_rows("rt-1"), target)
            orders = load_futures_orders("rt-1", target)
            trades = load_futures_trades("rt-1", target)
            daily = load_futures_account_daily("rt-1", target)
            self.assertEqual(len(orders), 2)
            self.assertEqual(orders[0]["open_close"], "OPEN")
            self.assertEqual(orders[1]["status"], "FILLED")
            self.assertEqual(len(trades), 2)
            self.assertAlmostEqual(trades[1]["realized_pnl"], 390.0)
            self.assertEqual([row["trade_date"] for row in daily], [D1, D2, D3])
            self.assertAlmostEqual(daily[-1]["balance"], 200573.8)
            self.assertEqual(load_futures_orders("other-run", target), [])


class AccountInvariantTest(unittest.TestCase):
    """引擎跑完后账户不变式：结清后 equity == balance、margin_occupied 归零。"""

    def test_flat_at_end(self):
        report = run_futures_backtest(
            {SYM: contract(three_day_bars(), three_day_daily())},
            base_config(), flat_fee_rules(), MARGINS,
            [TiltSignal(SYM, 1, "LONG"), TiltSignal(SYM, 3, "NEUTRAL")])
        # 结束强平兜底后必然空仓：占用为 0，权益 == 结存
        self.assertEqual(report.daily[-1]["margin_occupied"], 0.0)
        self.assertEqual(report.daily[-1]["equity"], report.daily[-1]["balance"])


if __name__ == "__main__":
    unittest.main()
