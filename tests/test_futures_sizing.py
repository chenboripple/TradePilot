"""期货风险测算测试（roadmap §5 口径的手算核验 + 三情形 gate）。

手算基准（rb / SHFE，futures_meta 表值）：乘数 10、每跳 10 元、缺省保证金率 7%、
往返手续费 6.2 元。价格 3104、ATR 60、止损倍数 1.0、单边滑点 1 跳时：

- 每手风险 = 60×10 + 6.2 + 2×1×10 = **626.2 元**
  （任务示例曾写作 686.2，系笔误：600 + 6.2 + 20 = 626.2，以 §5 公式为准）
- 名义 = 3104×10 = 31040；保证金估算 = 31040×7% = 2172.8
- 手数 = min(floor(2000/626.2)=3, floor(100000/2179.0)=45) = **3 手**
"""

import unittest

from ripple_tradePilot.data.futures_meta import PRODUCT_SPECS, product_spec
from ripple_tradePilot.risk.sizing import (
    RiskReport,
    SizingParams,
    affordable_lots,
    per_hand_risk,
    risk_report,
)

RB = PRODUCT_SPECS["RB"]
CU = PRODUCT_SPECS["CU"]
I = PRODUCT_SPECS["I"]

# 手算基准参数：止损倍数取 1，使止损距离 = ATR = 60（数字最干净）
BASE_PARAMS = SizingParams(
    risk_budget=2000.0, available_cash=100000.0, atr_stop_multiple=1.0, slippage_ticks=1.0
)


class PerHandRiskFormulaTest(unittest.TestCase):
    """§5 公式逐位核对：止损价差×乘数 + 往返费用 + 2×滑点跳数×每跳盈亏。"""

    def test_rb_hand_calculation(self):
        # 60×10 + 6.2 + 2×1×10 = 626.2
        self.assertAlmostEqual(per_hand_risk(RB, 60.0, 6.2, 1.0), 626.2)

    def test_other_products_hand_calculation(self):
        cases = [
            # cu：乘数 5、每跳 50 → 100×5 + 164.2 + 2×1×50 = 764.2
            (CU, 100.0, 164.2, 1.0, 764.2),
            # i：乘数 100、每跳 50 → 30×100 + 28.6 + 2×2×50 = 3228.6
            (I, 30.0, 28.6, 2.0, 3228.6),
        ]
        for spec, stop, fee, slip, expected in cases:
            with self.subTest(spec=spec.code):
                self.assertAlmostEqual(per_hand_risk(spec, stop, fee, slip), expected)

    def test_zero_slippage_and_fee(self):
        # 纯止损风险：60×10 = 600
        self.assertAlmostEqual(per_hand_risk(RB, 60.0, 0.0, 0.0), 600.0)


class AffordableLotsTest(unittest.TestCase):
    """可开手数 = 向下取整 min(风险/每手风险, 资金/每手占用)，负数夹 0。"""

    def test_rb_hand_calculation(self):
        # min(floor(2000/626.2)=3, floor(100000/(2172.8+6.2))=45) = 3
        self.assertEqual(affordable_lots(626.2, 2000.0, 100000.0, 2172.8, 6.2), 3)

    def test_cash_leg_binds(self):
        # 快照保证金口径：min(floor(2000/628)=3, floor(100000/2208)=45) = 3
        self.assertEqual(affordable_lots(628.0, 2000.0, 100000.0, 2200.0, 8.0), 3)
        # 资金只够一手多一点 → 1
        self.assertEqual(affordable_lots(628.0, 2000.0, 2500.0, 2200.0, 8.0), 1)

    def test_floor_not_round(self):
        # 2000/510 = 3.92… → 3（向下取整，不是四舍五入到 4）
        self.assertEqual(affordable_lots(510.0, 2000.0, 1e9, 2172.8, 6.2), 3)

    def test_non_positive_inputs_clamp_to_zero(self):
        cases = [
            ("每手风险 0", (0.0, 2000.0, 100000.0, 2172.8, 6.2)),
            ("每手风险负", (-626.2, 2000.0, 100000.0, 2172.8, 6.2)),
            ("预算负", (626.2, -2000.0, 100000.0, 2172.8, 6.2)),
            ("资金负", (626.2, 2000.0, -100000.0, 2172.8, 6.2)),
            ("占用非正", (626.2, 2000.0, 100000.0, 0.0, 0.0)),
        ]
        for name, args in cases:
            with self.subTest(name=name):
                self.assertEqual(affordable_lots(*args), 0)

    def test_non_finite_returns_zero(self):
        nan = float("nan")
        self.assertEqual(affordable_lots(nan, 2000.0, 100000.0, 2172.8, 6.2), 0)
        self.assertEqual(affordable_lots(626.2, nan, 100000.0, 2172.8, 6.2), 0)


class RiskReportNumbersTest(unittest.TestCase):
    """正常路径的数字逐项核对（近似保证金 = 缺省率估算路径）。"""

    def test_rb_full_report_with_default_margin(self):
        report = risk_report(
            RB,
            symbol="RB2610",
            price=3104.0,
            price_time="2026-09-18 21:15:00",
            atr=60.0,
            params=BASE_PARAMS,
            quote_ok=True,
        )
        self.assertIsInstance(report, RiskReport)
        self.assertEqual(report.symbol, "RB2610")
        self.assertEqual(report.price, 3104.0)
        self.assertEqual(report.price_time, "2026-09-18 21:15:00")
        self.assertAlmostEqual(report.notional_per_hand, 31040.0)
        self.assertAlmostEqual(report.margin_per_hand, 2172.8)  # 31040 × 7%
        self.assertTrue(report.margin_is_estimate)
        self.assertAlmostEqual(report.stop_distance, 60.0)  # 60 × 1.0
        self.assertAlmostEqual(report.stop_distance_pct, 60.0 / 3104.0 * 100.0, places=6)
        self.assertAlmostEqual(report.per_hand_risk, 626.2)
        self.assertEqual(report.lots, 3)
        self.assertTrue(report.executable)
        # 近似保证金只提醒、不拒绝
        self.assertEqual(len(report.reasons), 1)
        self.assertIn("保证金为近似估算", report.reasons[0])
        # 参数与规则版本回显（验收：注明参数与价格时间）
        self.assertEqual(report.params, BASE_PARAMS)
        self.assertEqual(report.rule_version, RB.rule_version)

    def test_rb_full_report_with_snapshot_margin(self):
        report = risk_report(
            RB,
            symbol="RB2610",
            price=3104.0,
            price_time="2026-09-18 21:15:00",
            atr=60.0,
            params=BASE_PARAMS,
            quote_ok=True,
            margin_per_hand=2200.0,
            round_trip_fee=8.0,
        )
        self.assertFalse(report.margin_is_estimate)
        self.assertAlmostEqual(report.margin_per_hand, 2200.0)
        # 每手风险 = 600 + 8 + 20 = 628；min(floor(2000/628)=3, floor(100000/2208)=45) = 3
        self.assertAlmostEqual(report.per_hand_risk, 628.0)
        self.assertEqual(report.lots, 3)
        self.assertTrue(report.executable)
        self.assertEqual(report.reasons, [])

    def test_max_lots_cap(self):
        capped = SizingParams(2000.0, 100000.0, 1.0, 1.0, max_lots=1)
        report = risk_report(
            RB, symbol="RB2610", price=3104.0, price_time="t", atr=60.0,
            params=capped, quote_ok=True,
        )
        self.assertEqual(report.lots, 1)
        self.assertTrue(report.executable)  # 管理性封顶不构成 gate 拒绝

    def test_default_two_x_atr_multiple(self):
        # 缺省倍数 2.0：止损 120 → 每手风险 120×10+6.2+20 = 1226.2 → 1 手
        report = risk_report(
            RB, symbol="RB2610", price=3104.0, price_time="t", atr=60.0,
            params=SizingParams(2000.0, 100000.0), quote_ok=True,
        )
        self.assertAlmostEqual(report.stop_distance, 120.0)
        self.assertAlmostEqual(report.per_hand_risk, 1226.2)
        self.assertEqual(report.lots, 1)


class RiskReportGatesTest(unittest.TestCase):
    """三情形 gate：行情过期 / 规则缺失 / 最小一手超预算 → 禁止可执行建议。"""

    def _report(self, **overrides):
        kwargs = dict(
            spec=RB, symbol="RB2610", price=3104.0, price_time="2026-09-18 21:15:00",
            atr=60.0, params=BASE_PARAMS, quote_ok=True,
        )
        kwargs.update(overrides)
        return risk_report(**kwargs)

    def test_gate_stale_quote(self):
        report = self._report(quote_ok=False)
        self.assertFalse(report.executable)
        self.assertTrue(any("行情过期" in r and "不可用" in r for r in report.reasons))
        # 数字仍然给出（供人工复核），但 gate 已禁止作为开仓建议
        self.assertEqual(report.lots, 3)

    def test_gate_missing_spec(self):
        report = self._report(spec=product_spec("TA"))  # 不在首期范围 → None
        self.assertIsNone(product_spec("TA"))
        self.assertFalse(report.executable)
        self.assertTrue(any("规则缺失" in r for r in report.reasons))
        self.assertEqual(report.rule_version, "")
        self.assertEqual(report.lots, 0)
        self.assertAlmostEqual(report.per_hand_risk, 0.0)

    def test_gate_missing_price_or_atr(self):
        for name, overrides in [
            ("price=None", dict(price=None)),
            ("price=0", dict(price=0.0)),
            ("price=-1", dict(price=-1.0)),
            ("atr=None", dict(atr=None)),
            ("atr=0", dict(atr=0.0)),
            ("atr=-5", dict(atr=-5.0)),
        ]:
            with self.subTest(name=name):
                report = self._report(**overrides)
                self.assertFalse(report.executable)
                joined = "；".join(report.reasons)
                if "price" in name:
                    self.assertIn("价格", joined)
                else:
                    self.assertIn("ATR", joined)

    def test_gate_min_lot_exceeds_risk_budget(self):
        # 预算 500 < 每手风险 626.2 → 一手都开不起，必须给出带数字的原因
        report = self._report(params=SizingParams(500.0, 100000.0, 1.0, 1.0))
        self.assertFalse(report.executable)
        self.assertEqual(report.lots, 0)
        joined = "；".join(report.reasons)
        self.assertIn("最小一手风险 626.2 元", joined)
        self.assertIn("500.0 元", joined)

    def test_gate_insufficient_cash(self):
        # 预算够但现金不够一手占用（保证金 2172.8 + 费用 6.2 = 2179.0）
        report = self._report(params=SizingParams(2000.0, 1000.0, 1.0, 1.0))
        self.assertFalse(report.executable)
        self.assertEqual(report.lots, 0)
        joined = "；".join(report.reasons)
        self.assertIn("2179.0 元", joined)
        self.assertIn("1000.0 元", joined)

    def test_gate_zero_budget_is_min_lot_gate(self):
        report = self._report(params=SizingParams(0.0, 100000.0, 1.0, 1.0))
        self.assertFalse(report.executable)
        self.assertEqual(report.lots, 0)
        self.assertIn("最小一手风险", "；".join(report.reasons))

    def test_multiple_gates_accumulate_reasons(self):
        # 行情过期 + 规则缺失同时发生：两个原因都要说清楚
        report = self._report(spec=None, quote_ok=False)
        self.assertFalse(report.executable)
        joined = "；".join(report.reasons)
        self.assertIn("行情过期", joined)
        self.assertIn("规则缺失", joined)

    def test_invalid_max_lots_rejects(self):
        report = self._report(
            params=SizingParams(2000.0, 100000.0, 1.0, 1.0, max_lots=0)
        )
        self.assertFalse(report.executable)
        self.assertEqual(report.lots, 0)
        self.assertTrue(any("手数上限" in r for r in report.reasons))


class RiskReportNeverRaisesTest(unittest.TestCase):
    """report 的职责是解释"为什么不可执行"，任何输入都不许抛异常。"""

    def test_garbage_inputs_return_report(self):
        cases = [
            ("全垃圾", dict(
                spec=None, symbol=None, price="3104", price_time=None,
                atr=float("nan"), params=SizingParams(float("nan"), float("inf")),
                quote_ok=False, margin_per_hand=-1.0, round_trip_fee=-2.0,
            )),
            ("参数类型错", dict(
                spec=RB, symbol="RB2610", price=3104.0, price_time="t", atr=60.0,
                params=SizingParams("2000", 100000.0), quote_ok=True,  # type: ignore[arg-type]
            )),
            ("负滑点", dict(
                spec=RB, symbol="RB2610", price=3104.0, price_time="t", atr=60.0,
                params=SizingParams(2000.0, 100000.0, 1.0, -0.5), quote_ok=True,
            )),
            ("零止损倍数", dict(
                spec=RB, symbol="RB2610", price=3104.0, price_time="t", atr=60.0,
                params=SizingParams(2000.0, 100000.0, 0.0, 1.0), quote_ok=True,
            )),
            ("负手续费快照", dict(
                spec=RB, symbol="RB2610", price=3104.0, price_time="t", atr=60.0,
                params=BASE_PARAMS, quote_ok=True, round_trip_fee=-6.2,
            )),
        ]
        for name, kwargs in cases:
            with self.subTest(name=name):
                report = risk_report(**kwargs)  # 不抛即通过
                self.assertFalse(report.executable)
                self.assertTrue(report.reasons)

    def test_non_positive_margin_snapshot_falls_back_to_estimate(self):
        report = risk_report(
            RB, symbol="RB2610", price=3104.0, price_time="t", atr=60.0,
            params=BASE_PARAMS, quote_ok=True, margin_per_hand=0.0,
        )
        # 快照异常 → 回退近似估算并注明，不静默当 0
        self.assertTrue(report.margin_is_estimate)
        self.assertAlmostEqual(report.margin_per_hand, 2172.8)
        joined = "；".join(report.reasons)
        self.assertIn("快照保证金非法", joined)
        self.assertIn("保证金为近似估算", joined)
        self.assertEqual(report.lots, 3)
        self.assertTrue(report.executable)  # 回退估算方向保守，不因此阻断三情形 gate


if __name__ == "__main__":
    unittest.main()
