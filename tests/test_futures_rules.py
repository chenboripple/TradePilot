"""费用与保证金规则层测试：手算黄金案例钉死数学，边界案例钉死区间语义。

覆盖四类口径（缺一即账务错）：
1. 三档查表（开/平今/平昨互不串档）与按手 vs 按成交额的数学；
2. 日期边界（两端含当日、次日切换、缺口 None）与重叠构造期拒绝；
3. gate 哲学（档缺 → fee_for None；垃圾输入 fee_for None / margin_for ValueError）；
4. approximate 近似口径标记在两个 Schedule 的透出（无规则 → False）。
"""

import math
import unittest

from ripple_tradePilot.backtest.futures_rules import (
    FeeRule,
    FeeSchedule,
    MarginRule,
    MarginSchedule,
    fee_for,
    margin_for,
)
from ripple_tradePilot.models.types import FuturesOffset


class FeeRuleValidationTest(unittest.TestCase):
    """FeeRule 构造期校验：数据错误早暴露，而不是成交时才算出 NaN/错价。"""

    def test_same_offset_both_modes_rejected(self):
        # WHY：同档按手与按成交额双设，意味着费用口径无法判定——静默取其一会让
        # 两个调用方算出两套账，必须在构造期报错。
        cases = {
            "open": dict(open_per_lot=3.0, open_by_ratio=0.0001),
            "close_today": dict(close_today_per_lot=5.0, close_today_by_ratio=0.0005),
            "close_yesterday": dict(close_yesterday_per_lot=4.0,
                                    close_yesterday_by_ratio=0.0001),
        }
        for tier, kwargs in cases.items():
            with self.subTest(tier=tier):
                with self.assertRaises(ValueError):
                    FeeRule(effective_from="20250101", effective_to=None, **kwargs)

    def test_rule_without_any_tier_rejected(self):
        # WHY：三档全缺的规则不可能产生任何合法费用，构造它本身就是 bug。
        with self.assertRaises(ValueError):
            FeeRule(effective_from="20250101", effective_to=None)

    def test_inverted_interval_rejected(self):
        # WHY：倒挂区间永远匹配不到日期，等于静默挖坑；构造期拒绝。
        with self.assertRaises(ValueError):
            FeeRule(effective_from="20250102", effective_to="20250101",
                    open_per_lot=1.5)

    def test_negative_or_nan_fee_value_rejected(self):
        # WHY：负费率 = 交易所倒贴成交，现实中不存在；NaN 会静默传染账本。
        # 0 合法（品种阶段性免收）。
        for field, bad in (("open_per_lot", -1.5), ("open_by_ratio", -0.0001),
                           ("open_per_lot", float("nan")),
                           ("close_today_by_ratio", float("inf"))):
            with self.subTest(field=field, value=bad):
                with self.assertRaises(ValueError):
                    FeeRule(effective_from="20250101", effective_to=None, **{field: bad})

    def test_bad_date_code_rejected(self):
        # WHY：区间比较全靠「零填充 YYYYMMDD 的字典序 == 时间序」，杂格式会静默
        # 查错规则，比报错危险，所以格式在构造期硬校验。
        for bad in ("2025-01-01", "202501", "2025/01/01", "20250230", 20250101):
            with self.subTest(bad_date=bad):
                with self.assertRaises(ValueError):
                    FeeRule(effective_from=bad, effective_to=None, open_per_lot=1.5)


class FeeForTest(unittest.TestCase):
    """fee_for 数学与档位映射：全部用可手算复核的黄金数。"""

    def test_three_tiers_distinct(self):
        # 三档各查各的：开仓按成交额、平今/平昨按手，互不串档。
        # 平今 5 元/手高于平昨 4 元/手，模拟交易所平今加收的常见形态。
        rule = FeeRule(effective_from="20250101", effective_to=None,
                       open_by_ratio=0.0001, close_today_per_lot=5.0,
                       close_yesterday_per_lot=4.0)
        self.assertAlmostEqual(
            fee_for(rule, FuturesOffset.OPEN, 3100.0, 1, 10.0), 3.1)
        self.assertAlmostEqual(
            fee_for(rule, FuturesOffset.CLOSE_TODAY, 3100.0, 2, 10.0), 10.0)
        self.assertAlmostEqual(
            fee_for(rule, FuturesOffset.CLOSE_YESTERDAY, 3100.0, 2, 10.0), 8.0)

    def test_notional_ratio_math_golden(self):
        # 手算黄金案例：RB 开 @3100 1 手 万1 → 3100 × 10 × 0.0001 = 3.1 元
        rule = FeeRule(effective_from="20250101", effective_to=None,
                       open_by_ratio=0.0001)
        self.assertAlmostEqual(
            fee_for(rule, FuturesOffset.OPEN, 3100.0, 1, 10.0), 3.1)

    def test_per_lot_math_golden(self):
        # 手算黄金案例：豆粕口径 1.5 元/手 × 3 手 = 4.5 元（按手与价格、乘数无关）
        rule = FeeRule(effective_from="20250101", effective_to=None,
                       open_per_lot=1.5)
        self.assertAlmostEqual(
            fee_for(rule, FuturesOffset.OPEN, 3200.0, 3, 10.0), 4.5)

    def test_close_maps_to_close_yesterday_tier(self):
        # 撮合层能拆今昨时应显式传 CLOSE_TODAY/CLOSE_YESTERDAY；只有拆不了才传
        # plain CLOSE——钉死映射到平昨档，少传一个枚举值也有确定口径。
        rule = FeeRule(effective_from="20250101", effective_to=None,
                       close_today_per_lot=5.0, close_yesterday_per_lot=4.0)
        self.assertAlmostEqual(
            fee_for(rule, FuturesOffset.CLOSE, 3100.0, 2, 10.0), 8.0)

    def test_missing_tier_returns_none(self):
        # 规则缺失 gate：该 offset 没定价 → None → 撮合层禁止成交。
        # WHY 不用 0 填：0 费用会把不该成交的信号放进回测，错账比少成交危害大。
        rule = FeeRule(effective_from="20250101", effective_to=None,
                       open_per_lot=3.0)
        self.assertIsNone(fee_for(rule, FuturesOffset.CLOSE, 3100.0, 1, 10.0))
        self.assertIsNone(fee_for(rule, FuturesOffset.CLOSE_TODAY, 3100.0, 1, 10.0))
        self.assertIsNone(fee_for(rule, FuturesOffset.CLOSE_YESTERDAY, 3100.0, 1, 10.0))

    def test_garbage_inputs_return_none(self):
        # WHY 返回 None 而不是抛错（与 margin_for 相反的钉死分工）：费用算不出
        # 说明这笔成交本身可疑，交给撮合层 gate 掉即可，不应炸掉整个回测。
        rule = FeeRule(effective_from="20250101", effective_to=None,
                       open_by_ratio=0.0001)
        cases = {
            "price=0": dict(price=0.0, lots=1, multiplier=10.0),
            "price<0": dict(price=-3100.0, lots=1, multiplier=10.0),
            "price=nan": dict(price=float("nan"), lots=1, multiplier=10.0),
            "price=inf": dict(price=float("inf"), lots=1, multiplier=10.0),
            "lots=0": dict(price=3100.0, lots=0, multiplier=10.0),
            "lots=nan": dict(price=3100.0, lots=float("nan"), multiplier=10.0),
            "multiplier=0": dict(price=3100.0, lots=1, multiplier=0.0),
            "multiplier=nan": dict(price=3100.0, lots=1, multiplier=float("nan")),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                self.assertIsNone(fee_for(rule, FuturesOffset.OPEN, **kwargs))


class FeeScheduleTest(unittest.TestCase):
    """区间语义：两端含当日、次日切换、缺口 None、重叠构造期拒绝。"""

    def test_date_boundaries_and_switch(self):
        # WHY 两端都含当日：交易所公告「自 X 日起执行」当日即按新规收；漏掉当日
        # 会把调费日的账算到旧费率上。传入乱序是为了钉死构造期内部排序。
        old = FeeRule(effective_from="20250101", effective_to="20250310",
                      open_per_lot=3.0)
        new = FeeRule(effective_from="20250311", effective_to=None,
                      open_per_lot=4.0)
        schedule = FeeSchedule([new, old])
        self.assertIs(schedule.rule_on("20250101"), old)   # effective_from 当日生效
        self.assertIs(schedule.rule_on("20250310"), old)   # effective_to 当日仍生效
        self.assertIs(schedule.rule_on("20250311"), new)   # 次日切换新规则
        self.assertIsNone(schedule.rule_on("20241231"))    # 起点之前 → 缺口

    def test_gap_returns_none(self):
        # WHY 缺口 → None：二月没有任何官方规则，宁可禁成交也不猜一个价。
        a = FeeRule(effective_from="20250101", effective_to="20250131",
                    open_per_lot=1.0)
        b = FeeRule(effective_from="20250301", effective_to=None, open_per_lot=2.0)
        schedule = FeeSchedule([a, b])
        self.assertIsNone(schedule.rule_on("20250215"))
        self.assertIs(schedule.rule_on("20250131"), a)
        self.assertIs(schedule.rule_on("20250301"), b)

    def test_overlap_rejected(self):
        # WHY 构造期拒绝重叠：重叠区间意味着同一天两个价，静默取先/取后都会让
        # 两个调用方对不上账。三种形态（前包后/后包前/交错）都必须炸。
        shapes = {
            "前包后": [
                FeeRule(effective_from="20250101", effective_to="20251231",
                        open_per_lot=1.0),
                FeeRule(effective_from="20250601", effective_to="20250630",
                        open_per_lot=2.0),
            ],
            "后包前": [
                FeeRule(effective_from="20250601", effective_to="20250630",
                        open_per_lot=2.0),
                FeeRule(effective_from="20250101", effective_to="20251231",
                        open_per_lot=1.0),
            ],
            "交错": [
                FeeRule(effective_from="20250101", effective_to="20250301",
                        open_per_lot=1.0),
                FeeRule(effective_from="20250215", effective_to="20250601",
                        open_per_lot=2.0),
            ],
            "同日双生效": [
                # 两端都含当日 ⇒ 20250301 同天两个价，也是重叠
                FeeRule(effective_from="20250101", effective_to="20250301",
                        open_per_lot=1.0),
                FeeRule(effective_from="20250301", effective_to="20250601",
                        open_per_lot=2.0),
            ],
        }
        for shape, rules in shapes.items():
            with self.subTest(shape=shape):
                with self.assertRaises(ValueError):
                    FeeSchedule(rules)

    def test_empty_schedule_is_all_gates(self):
        # 空表合法但全线 gate：语义与缺口一致（「明确无规则」不是构造错误），
        # 每个日期 rule_on → None、is_approximate_on → False。
        schedule = FeeSchedule([])
        self.assertIsNone(schedule.rule_on("20250101"))
        self.assertFalse(schedule.is_approximate_on("20250101"))

    def test_malformed_lookup_date_rejected(self):
        # 查找日期同样硬校验：杂格式静默查错规则比报错危险。
        schedule = FeeSchedule([
            FeeRule(effective_from="20250101", effective_to=None, open_per_lot=1.5)])
        with self.assertRaises(ValueError):
            schedule.rule_on("2025-01-01")


class ApproximateFlagTest(unittest.TestCase):
    """approximate 近似口径标记（P0 遗留要求）在两个 Schedule 的透出。"""

    def test_fee_schedule_approximate_flag(self):
        # 历史段用快照近似、近段有官方公告：标记必须随区间切换透出，
        # 报告层据此挂「近似」告警，禁止近似冒充官方。
        approx = FeeRule(effective_from="20240101", effective_to="20250630",
                         approximate=True, open_per_lot=3.0)
        exact = FeeRule(effective_from="20250701", effective_to=None,
                        approximate=False, open_per_lot=3.1)
        schedule = FeeSchedule([approx, exact])
        self.assertTrue(schedule.is_approximate_on("20250301"))
        self.assertFalse(schedule.is_approximate_on("20250701"))
        # 无规则日期 → False：近似标记只描述「已生效规则的口径来源」，
        # 缺规则告警由 rule_on is None 这条 gate 负责，两种语义不混用。
        self.assertFalse(schedule.is_approximate_on("20231231"))

    def test_margin_schedule_approximate_flag(self):
        approx = MarginRule(effective_from="20240101", effective_to="20250630",
                            rate=0.13, approximate=True)
        exact = MarginRule(effective_from="20250701", effective_to=None, rate=0.12)
        schedule = MarginSchedule([approx, exact])
        self.assertTrue(schedule.is_approximate_on("20250301"))
        self.assertFalse(schedule.is_approximate_on("20250701"))
        self.assertFalse(schedule.is_approximate_on("20231231"))


class MarginRuleAndScheduleTest(unittest.TestCase):
    """MarginRule 校验与 MarginSchedule 区间语义（与费率侧同一套钉死口径）。"""

    def test_invalid_rate_rejected(self):
        # WHY：rate 是资金占用比例——0/负值 = 免保证金（不存在），>1 = 保证金比
        # 名义价值还多（数据错误），NaN 与任何比较都是 False 会静默穿过，须显式挡。
        for bad in (0.0, -0.05, 1.0001, float("nan"), float("inf"), float("-inf")):
            with self.subTest(rate=bad):
                with self.assertRaises(ValueError):
                    MarginRule(effective_from="20250101", effective_to=None, rate=bad)

    def test_valid_rate_bounds_accepted(self):
        # (0, 1] 闭端合法：1.0（全额保证金）与极小正率都在域内。
        MarginRule(effective_from="20250101", effective_to=None, rate=1.0)
        MarginRule(effective_from="20250101", effective_to=None, rate=0.0001)

    def test_inverted_interval_rejected(self):
        with self.assertRaises(ValueError):
            MarginRule(effective_from="20250102", effective_to="20250101", rate=0.13)

    def test_rate_on_boundaries_and_gap(self):
        # 同 FeeSchedule：from 当日生效、to 当日仍生效、次日切换、缺口 → None
        a = MarginRule(effective_from="20250101", effective_to="20250310", rate=0.13)
        b = MarginRule(effective_from="20250311", effective_to=None, rate=0.09)
        schedule = MarginSchedule([b, a])
        self.assertAlmostEqual(schedule.rate_on("20250101"), 0.13)
        self.assertAlmostEqual(schedule.rate_on("20250310"), 0.13)
        self.assertAlmostEqual(schedule.rate_on("20250311"), 0.09)
        self.assertIsNone(schedule.rate_on("20241231"))

    def test_overlap_rejected(self):
        with self.assertRaises(ValueError):
            MarginSchedule([
                MarginRule(effective_from="20250101", effective_to="20250301",
                           rate=0.13),
                MarginRule(effective_from="20250215", effective_to="20250601",
                           rate=0.09),
            ])


class MarginForTest(unittest.TestCase):
    """margin_for 数学与垃圾输入的钉死口径（抛 ValueError）。"""

    def test_golden_math(self):
        # 手算黄金案例：0.13 × 3100 × 10 × 2 手 = 8060 元
        self.assertAlmostEqual(margin_for(0.13, 3100.0, 2, 10.0), 8060.0)

    def test_garbage_raises(self):
        # WHY 抛 ValueError（与 fee_for 返回 None 相反的钉死分工）：账本层依赖
        # 保证金一定可算（调用前已过 rate_on 的 None 检查），还算不出说明是
        # 编程错误，直接炸掉暴露，绝不让 None/NaN 流进权益曲线。
        cases = {
            "rate=0": dict(rate=0.0, price=3100.0, lots=2, multiplier=10.0),
            "rate<0": dict(rate=-0.1, price=3100.0, lots=2, multiplier=10.0),
            "rate=nan": dict(rate=float("nan"), price=3100.0, lots=2, multiplier=10.0),
            "price=0": dict(rate=0.13, price=0.0, lots=2, multiplier=10.0),
            "price=nan": dict(rate=0.13, price=float("nan"), lots=2, multiplier=10.0),
            "lots=0": dict(rate=0.13, price=3100.0, lots=0, multiplier=10.0),
            "lots=nan": dict(rate=0.13, price=3100.0, lots=float("nan"),
                             multiplier=10.0),
            "multiplier=0": dict(rate=0.13, price=3100.0, lots=2, multiplier=0.0),
            "multiplier=inf": dict(rate=0.13, price=3100.0, lots=2,
                                   multiplier=float("inf")),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    margin_for(**kwargs)


if __name__ == "__main__":
    unittest.main()
