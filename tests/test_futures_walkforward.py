"""期货滚动样本外验证测试（roadmap P2「滚动样本外验证 + 保留集」）。

合成阶梯序列每天涨 1 点：任一窗口在首个可评估 bar 即 LONG → 每段恰好
「次 bar 开盘+1 跳买入、段末收盘−1 跳强平」两笔成交，权益可手算复核：
PnL = (段末收盘−1 − 次bar开盘−1) × 乘数 − 双边费用。
"""

import math
import unittest
from datetime import date, timedelta
from typing import List

from ripple_tradePilot.backtest.futures_engine import ContractInput, EngineConfig
from ripple_tradePilot.backtest.futures_rules import (
    FeeRule,
    FeeSchedule,
    MarginRule,
    MarginSchedule,
)
from ripple_tradePilot.backtest.futures_walkforward import (
    WalkforwardParams,
    futures_walkforward,
    main_timeline_signals,
)

SYM = "RB2610.SHFE"
DAYS = 100
_START = date(2026, 1, 5)


def date_of(i: int) -> str:
    # 连续真实日历日（YYYYMMDD）：规则层硬校验真实日期，且等长字典序 == 时间序
    return (_START + timedelta(days=i)).strftime("%Y%m%d")


def staircase_bars() -> List[dict]:
    bars = []
    for i in range(DAYS):
        bars.append({
            "trade_date": date_of(i), "bar_time": f"{date_of(i)} 10:30:00",
            "open": 100 + i, "high": 101 + i, "low": 100 + i, "close": 100.5 + i,
            "volume": 100000,
        })
    return bars


def staircase_dailies() -> List[dict]:
    return [{"trade_date": date_of(i), "settle": 100.5 + i, "volume": 100000,
             "hold": 50000} for i in range(DAYS)]


FEES = FeeSchedule([FeeRule("20200101", None, open_per_lot=3.1,
                            close_today_per_lot=3.1, close_yesterday_per_lot=3.1)])
MARGINS = MarginSchedule([MarginRule("20200101", None, 0.13)])
CONFIG = EngineConfig(initial_cash=200000.0, slippage_ticks=1,
                      max_volume_participation=0.1, run_id="wf-test")
PARAMS = WalkforwardParams(entry_windows=(10, 20), test_blocks=3, holdout_ratio=0.2)


def run_staircase(params=None):
    return futures_walkforward(
        {SYM: ContractInput(SYM, 10.0, 1.0, staircase_bars(), staircase_dailies())},
        CONFIG, FEES, MARGINS, None, params or PARAMS)


class SegmentBoundaryTest(unittest.TestCase):
    """段边界：训练严格早于测试、保留集与所有段不重叠（无前视的结构保证）。"""

    def test_boundaries_and_holdout_isolation(self):
        report = run_staircase()
        kinds = [seg.kind for seg in report.segments]
        self.assertEqual(kinds, ["test", "test", "test", "holdout"])
        holdout = report.holdout
        self.assertEqual(holdout.test_start, date_of(80))
        self.assertEqual(holdout.test_end, date_of(99))
        for seg in report.segments[:-1]:
            with self.subTest(seg=seg.index):
                self.assertLess(seg.train_end, seg.test_start)
                self.assertLessEqual(seg.test_end, date_of(79))   # 不碰保留集
                self.assertEqual(seg.train_start, date_of(0))     # 锚定扩展训练
        # 训练集也不含保留集：holdout 的训练止于保留集首日之前
        self.assertEqual(report.holdout.train_end, date_of(79))

    def test_insufficient_dates_rejected(self):
        # 模块护栏：交易日数 < test_blocks×4 直接拒绝（26×4=104 > 100 天）
        with self.assertRaises(ValueError):
            run_staircase(WalkforwardParams(entry_windows=(10,), test_blocks=26,
                                            holdout_ratio=0.2))

    def test_bad_ratio_rejected(self):
        with self.assertRaises(ValueError):
            run_staircase(WalkforwardParams(entry_windows=(10,), test_blocks=1,
                                            holdout_ratio=1.5))


class TrendingSelectionTest(unittest.TestCase):
    """阶梯上涨：小窗口训练收益更高（信号更早）→ 每段选 10；段内权益可手算。"""

    def test_small_window_wins_and_equity_hand_computed(self):
        report = run_staircase()
        self.assertEqual([seg.chosen_entry_window for seg in report.segments],
                         [10, 10, 10, 10])
        for seg in report.segments:
            with self.subTest(kind=seg.kind, index=seg.index):
                # 首体 bar 出信号 → 次日开盘+1 跳买入 → 段末收盘−1 跳强平。
                # 夹具收盘价是 x.5（半 tick）：卖出滑点后按 tick 网格向下
                # 取整（138.5 → 138，吃亏方向），手算必须算上这一步。
                first_body = 20 * (seg.index + 1) if seg.kind == "test" else 80
                buy = 100 + first_body + 1 + 1          # 次日开盘 100+日 + 1 跳
                sell = math.floor(100.5 + (first_body + 19) - 1)
                expected = 200000.0 + (sell - buy) * 10 - 6.2
                self.assertAlmostEqual(seg.test_equity, expected)
                self.assertEqual(seg.fills, 2)
        holdout = report.holdout
        self.assertAlmostEqual(holdout.test_return,
                               holdout.test_equity / 200000.0 - 1)
        # OOS 总收益 = 三段复利
        compounded = 1.0
        for seg in report.segments[:-1]:
            compounded *= seg.test_equity / 200000.0
        self.assertAlmostEqual(report.summary["oos_total_return"], compounded - 1)
        self.assertEqual(report.summary["test_fills_total"], 6)

    def test_manifest_records_data_params_version(self):
        report = run_staircase()
        manifest = report.manifest
        self.assertEqual(manifest["data_start"], date_of(0))
        self.assertEqual(manifest["data_end"], date_of(99))
        self.assertEqual(manifest["holdout_start"], date_of(80))
        self.assertEqual(manifest["entry_windows"], [10, 20])
        self.assertEqual(manifest["warmup_bars"], 35)   # max(20) + 15
        self.assertFalse(manifest["fee_approximate"])
        self.assertFalse(manifest["margin_approximate"])
        self.assertIn("donchian-grid", str(manifest["strategy_version"]))


class FlatSelectionTest(unittest.TestCase):
    """全程横盘：所有窗口无交易 → 训练权益全平 → 平手取最小窗口，OOS 零交易。"""

    def test_tie_breaks_to_smallest_window(self):
        bars = [{"trade_date": date_of(i), "bar_time": f"{date_of(i)} 10:30:00",
                 "open": 100, "high": 100, "low": 100, "close": 100,
                 "volume": 100000} for i in range(DAYS)]
        dailies = [{"trade_date": date_of(i), "settle": 100, "volume": 100000,
                    "hold": 50000} for i in range(DAYS)]
        report = futures_walkforward(
            {SYM: ContractInput(SYM, 10.0, 1.0, bars, dailies)},
            CONFIG, FEES, MARGINS, None, PARAMS)
        self.assertEqual([seg.chosen_entry_window for seg in report.segments],
                         [10, 10, 10, 10])
        for seg in report.segments:
            self.assertEqual(seg.fills, 0)
            self.assertAlmostEqual(seg.test_equity, 200000.0)
        self.assertEqual(report.summary["oos_total_return"], 0.0)


class MainTimelineSignalsTest(unittest.TestCase):
    """主力时间线信号：只认主力日的 bar，方向变化才发事件。"""

    def test_signals_only_on_main_dates(self):
        bars = staircase_bars()
        main_dates = [date_of(i) for i in range(50, DAYS)]   # 仅后半段是主力
        signals = main_timeline_signals(bars, 10, symbol=SYM, main_dates=main_dates)
        # 前半段不评估；首个主力日（含预热历史）即 LONG，之后无变化 → 单事件
        self.assertEqual([(s.bar_index, s.direction) for s in signals],
                         [(50, "LONG")])

    def test_full_timeline_when_no_main_filter(self):
        signals = main_timeline_signals(staircase_bars(), 10, symbol=SYM)
        # 首个可评估 bar：len≥max(entry+1, ATR)=14 → index 13（阶梯日日破高）
        self.assertEqual([(s.bar_index, s.direction) for s in signals], [(13, "LONG")])
        # window=20 时首个可评估 bar 后移（ATR 已满足，受 entry 窗约束）
        signals20 = main_timeline_signals(staircase_bars(), 20, symbol=SYM)
        self.assertEqual(signals20[0].bar_index, 20)


class RollContinuityTest(unittest.TestCase):
    """段内主力切换：换月必须传进引擎（平旧开新），不能让旧仓悬挂到段末。

    回归背景：``_run_once`` 曾漏传 ``roll_schedule`` → 段内永不换月——旧主力
    仓位无人平（信号层 main_dates 门控只抑制新信号），新主力照常开仓：同品种
    双仓、双保证金、漏掉全部换月成本，OOS 段收益失真。
    """

    OLD = "RB2610.SHFE"
    NEW = "RB2701.SHFE"

    def _contracts(self):
        from ripple_tradePilot.backtest.futures_engine import build_roll_schedule
        bars = staircase_bars()
        dailies = []
        for i in range(DAYS):
            # 第 50 天起量能反转 → build_roll_schedule 判第 51 天起主力 = NEW
            old_vol = 60000 if i < 50 else 20000
            new_vol = 20000 if i < 50 else 60000
            dailies.append({
                "trade_date": date_of(i), "settle": 100.5 + i,
                "old_volume": old_vol, "new_volume": new_vol,
                "hold": 50000,
            })
        contracts = {}
        daily_by_symbol = {}
        for symbol, vol_key in ((self.OLD, "old_volume"), (self.NEW, "new_volume")):
            rows = [{"trade_date": d["trade_date"], "settle": d["settle"],
                     "volume": d[vol_key], "hold": d["hold"]} for d in dailies]
            contracts[symbol] = ContractInput(symbol, 10.0, 1.0,
                                              [dict(bar) for bar in bars], rows)
            daily_by_symbol[symbol] = rows
        return contracts, build_roll_schedule(daily_by_symbol)

    def test_roll_schedule_crossover_day_51(self):
        # 夹具自检：主力序列第 51 天起切到 NEW（判 D 主力用 D-1 的量能）
        _, roll = self._contracts()
        self.assertEqual(roll[50][1], self.OLD)
        self.assertEqual(roll[51][1], self.NEW)
        self.assertEqual(roll[-1][1], self.NEW)

    def test_segment_covering_crossover_rolls(self):
        contracts, roll = self._contracts()
        report = futures_walkforward(contracts, CONFIG, FEES, MARGINS, roll, PARAMS)
        # 段边界（DAYS=100, holdout 20%, 3 块）：test0=20-39, test1=40-59,
        # test2=60-79, holdout=80-99 → 换月日 51 只落在 test1
        by_index = {seg.index: seg for seg in report.segments}
        self.assertEqual(by_index[0].rolls, 0)
        self.assertEqual(by_index[1].rolls, 1)   # 修前此处为 0（漏传的直接证据）
        self.assertEqual(by_index[2].rolls, 0)
        self.assertEqual(report.holdout.rolls, 0)
        self.assertEqual(report.summary["test_rolls_total"], 1)
        # 换月腿进了成交与费用（平旧 + 开新各 1 笔 is_roll 腿，段内不再双仓）
        seg1 = by_index[1]
        self.assertGreaterEqual(seg1.fills, 4)   # 开旧 + 平旧(roll) + 开新(roll) + 段末强平

    def test_training_run_also_rolls_when_crossover_inside(self):
        # test2 的训练集（0-59）与 holdout 的训练集（0-79）都覆盖换月日 51：
        # 选参训练同样经引擎换月，不因「训练段」而悬挂旧仓
        contracts, roll = self._contracts()
        report = futures_walkforward(contracts, CONFIG, FEES, MARGINS, roll, PARAMS)
        # 训练权益不含双仓泄漏：所有段都正常选出窗口并产出有限权益
        for seg in report.segments:
            with self.subTest(kind=seg.kind, index=seg.index):
                self.assertIsNotNone(seg.chosen_entry_window)
                self.assertTrue(math.isfinite(seg.train_equity))
                self.assertTrue(math.isfinite(seg.test_equity))


if __name__ == "__main__":
    unittest.main()
