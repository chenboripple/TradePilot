"""期货品种元数据测试（P0 §1/§3 的机器化核验）。

每跳盈亏期望值来自 ``futures_comm_info`` 2026-09-18 快照的「每跳毛利」列——
这是**独立源交叉核验**：手维护规格错了这里就红。
"""

import unittest

from ripple_tradePilot.data.futures_meta import (
    ContractIdentity,
    PRODUCT_SPECS,
    canonical_symbol,
    margin_estimate,
    notional,
    parse_contract_symbol,
    product_spec,
    products_by_exchange,
    tick_value,
)


class FuturesMetaTest(unittest.TestCase):
    def test_tick_values_match_independent_snapshot(self):
        # comm_info 2026-09-18「每跳毛利」：rb/hc/m=10，i/cu=50（P0 核对记录）
        expected = {"RB": 10.0, "HC": 10.0, "CU": 50.0, "I": 50.0, "M": 10.0}
        self.assertEqual(set(PRODUCT_SPECS), set(expected))  # 首期范围钉死 5 品种
        for product, value in expected.items():
            self.assertEqual(tick_value(PRODUCT_SPECS[product]), value, product)

    def test_night_sessions_verified_by_probe(self):
        # P0 §4.1：rb/hc/i/m 夜盘 bar 小时分布 {22,23}；cu 含 {22,23,00,01}
        for code in ("RB", "HC", "I", "M"):
            spec = PRODUCT_SPECS[code]
            self.assertEqual((spec.night_start, spec.night_end), ("21:00", "23:00"), code)
            self.assertFalse(spec.night_end_next_day, code)
        cu = PRODUCT_SPECS["CU"]
        self.assertEqual((cu.night_start, cu.night_end), ("21:00", "01:00"))
        self.assertTrue(cu.night_end_next_day)

    def test_parse_contract_symbol_four_digit(self):
        identity = parse_contract_symbol("rb2610")  # 大小写不敏感
        self.assertEqual(
            identity,
            ContractIdentity("RB2610", "RB2610.SHFE", "RB", "SHFE", 2026, 10),
        )
        self.assertEqual(canonical_symbol("RB2610"), "RB2610.SHFE")

    def test_parse_contract_symbol_czce_three_digit(self):
        # 郑商所 3 位月份码：TA701 = 2027-01（品种不在首期范围 → 报错而非猜测）
        with self.assertRaises(ValueError) as ctx:
            parse_contract_symbol("TA701")
        self.assertIn("不在首期范围", str(ctx.exception))

    def test_parse_rejects_garbage(self):
        for bad in ("RB26", "RB26100", "2610", "RB", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_contract_symbol(bad)
        with self.assertRaises(ValueError):  # 月份 13 非法
            parse_contract_symbol("RB2613")

    def test_notional_and_margin_estimate(self):
        rb = PRODUCT_SPECS["RB"]
        self.assertEqual(notional(rb, 3104.0), 31040.0)  # P0 表：主力名义 31,040
        self.assertAlmostEqual(margin_estimate(rb, 3104.0), 31040.0 * 0.07)
        # 显式费率覆盖缺省（快照在位时由调用方传入）
        self.assertAlmostEqual(margin_estimate(rb, 3104.0, 0.05), 31040.0 * 0.05)

    def test_unknown_product_returns_none(self):
        self.assertIsNone(product_spec("TA"))
        self.assertIsNotNone(product_spec("rb"))

    def test_products_by_exchange_covers_both(self):
        by_exchange = products_by_exchange()
        self.assertEqual(by_exchange, {"DCE": ["I", "M"], "SHFE": ["CU", "HC", "RB"]})


if __name__ == "__main__":
    unittest.main()
