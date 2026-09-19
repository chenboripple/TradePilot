"""期货账户账本测试（P2「逐笔账目核对」——每个数字都手算可复现）。

手算基准（RB / SHFE，乘数取常数 10，与 futures_meta 表值一致）：
初始资金 200000、margin_rate=0.13、开/平手续费每笔 3.1。

- 开多 1 手 @3100：保证金 3100×10×0.13 = **4030**；balance = 200000−3.1 = 199996.9
- 结算 @3150：盯市 (3150−3100)×10 = **+500** → balance 200496.9；
  保证金重置 3150×10×0.13 = **4095**；昨仓成本 := 3150
- 次日平昨 @3160：(3160−3150)×10 = **+100**
- 总账不变式：结算 +500 与平昨 +100 合计恰为全程价差 (3160−3100)×10 = 600，
  证明「逐日盯市 + 成本重置」不重复计盈亏（roadmap §P2 钉死的验收点）。

任务示例笔误更正（与钉死公式为准，同 sizing 测试先例）：
- 用例 6 今档写 20，实为 (3160−3140)×10 = **200**（其昨档 100 已含 ×10，
  两档口径必须一致）；合计 realized = 100 + 200 = **300**。
- 用例 7 平今写 40，实为 (3150−3110)×10 = **400**（40 是点差，未乘乘数）。
"""

import unittest

from ripple_tradePilot.backtest.futures_account import (
    CloseBreakdown,
    FuturesAccount,
    FuturesFill,
    PositionState,
)
from ripple_tradePilot.models.types import FuturesDirection, FuturesOffset

RB = "RB2610.SHFE"
HC = "HC2610.SHFE"
LONG = FuturesDirection.LONG
SHORT = FuturesDirection.SHORT

INITIAL = 200000.0
MULT = 10.0          # RB 乘数
HC_MULT = 5.0        # HC 乘数（缺价隔离用例）
RATE = 0.13          # 保证金率
FEE = 3.1            # 开/平各 3.1

D1 = "20260901"
D2 = "20260902"


def open_fill(price, lots, symbol=RB, direction=LONG, trade_date=D1, fee=FEE, order_id="o1"):
    return FuturesFill(
        order_id=order_id,
        symbol=symbol,
        direction=direction,
        offset=FuturesOffset.OPEN,
        price=price,
        lots=lots,
        fee=fee,
        trade_date=trade_date,
    )


def close_fill(price, lots, offset, symbol=RB, direction=LONG, trade_date=D1, fee=FEE, order_id="c1"):
    return FuturesFill(
        order_id=order_id,
        symbol=symbol,
        direction=direction,
        offset=offset,
        price=price,
        lots=lots,
        fee=fee,
        trade_date=trade_date,
    )


class OpenLongTest(unittest.TestCase):
    """用例 1：开多后费用入账、保证金冻结、权益与可用。"""

    def test_open_long_accounting(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1), MULT, RATE)

        # 费用当日直接从结存扣：200000 − 3.1
        self.assertAlmostEqual(acc.balance, 199996.9)
        # 新仓按开仓价计保证金：3100×10×1×0.13 = 4030
        self.assertAlmostEqual(acc.margin_occupied, 4030.0)
        # 盘中 mark 恰为开仓价 → 未实现为 0，权益 = 结存
        self.assertAlmostEqual(acc.equity({RB: 3100.0}), 199996.9)
        # 可用 = 权益 − 占用：199996.9 − 4030
        self.assertAlmostEqual(acc.available({RB: 3100.0}), 195966.9)

        pos = acc.position(RB, LONG)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.today_lots, 1)
        self.assertAlmostEqual(pos.today_cost, 3100.0)
        self.assertEqual(pos.yesterday_lots, 0)
        # 今仓尚未毕业，昨档成本应为零而非陈旧值
        self.assertAlmostEqual(pos.yesterday_cost, 0.0)


class DailySettlementTest(unittest.TestCase):
    """用例 2：结算价盯市兑现、成本重置、今仓毕业。"""

    def test_settle_marks_to_market_and_resets_cost(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1), MULT, RATE)

        result = acc.settle(D1, {RB: 3150.0}, {RB: MULT}, RATE)

        # 持仓盈亏 = (3150−3100)×10×1 = +500，当日盯市当日兑现
        self.assertAlmostEqual(result.position_pnl, 500.0)
        self.assertAlmostEqual(acc.balance, 200496.9)
        self.assertEqual(result.errors, [])

        pos = acc.position(RB, LONG)
        self.assertEqual(pos.yesterday_lots, 1)
        self.assertAlmostEqual(pos.yesterday_cost, 3150.0)  # 成本重置为结算价
        self.assertEqual(pos.today_lots, 0)                 # 今仓毕业
        self.assertAlmostEqual(pos.today_cost, 0.0)

        # 保证金按结算价重置：3150×10×1×0.13 = 4095
        self.assertAlmostEqual(acc.margin_occupied, 4095.0)
        self.assertAlmostEqual(result.margin_occupied_after, 4095.0)
        # 无 marks 时权益 = 结存（结算后成本=结算价，无未实现——不变式）
        self.assertAlmostEqual(acc.equity(), acc.balance)
        self.assertAlmostEqual(acc.equity(), 200496.9)
        # 结算记录留档
        self.assertEqual(len(acc.settlements), 1)


class NoDoubleCountingLedgerTest(unittest.TestCase):
    """用例 3（钉死验收点）：结算盯市 + 平昨盈亏 合计 = 全程价差，无重复计入。

    结算 +500 与平昨 +100 合计 600，恰等于开仓价到平仓价的全程价差
    (3160−3100)×10——若结算后忘记重置成本，平昨会再按 3100 算出 600，
    总账将多计 500。此测试守住 roadmap §P2「避免结算盈亏与平仓盈亏
    重复计入」。
    """

    def test_settlement_then_close_totals_to_full_spread(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1), MULT, RATE)
        acc.settle(D1, {RB: 3150.0}, {RB: MULT}, RATE)
        acc.apply_fill(
            close_fill(3160.0, 1, FuturesOffset.CLOSE_YESTERDAY, trade_date=D2), MULT, RATE
        )

        # 平昨：(3160−3150)×10 = +100，扣费 3.1
        self.assertAlmostEqual(acc.balance, 200496.9 + 100.0 - 3.1)
        self.assertAlmostEqual(acc.realized_today(D2), 100.0)
        # 持仓清零、保证金清零
        self.assertIsNone(acc.position(RB, LONG))
        self.assertEqual(acc.positions_snapshot(), [])
        self.assertAlmostEqual(acc.margin_occupied, 0.0)

        # 总账校验：200000 + (3160−3100)×10 − 3.1 − 3.1 = 200593.8
        with self.subTest(case="总账 = 初始资金 + 全程价差 − 全部费用"):
            self.assertAlmostEqual(acc.balance, 200593.8)
            # 独立公式互证（不是用同一代码路径算出来比大小）
            self.assertAlmostEqual(acc.balance, INITIAL + (3160.0 - 3100.0) * 10.0 - 2 * FEE)
            # 两条腿合计恰为全程价差 600
            self.assertAlmostEqual(500.0 + 100.0, (3160.0 - 3100.0) * 10.0)


class SameDayCloseTodayTest(unittest.TestCase):
    """用例 4：同日开平今——平今盈亏按今开仓均价基准。"""

    def test_open_then_close_today(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1), MULT, RATE)
        breakdown = acc.apply_fill(
            close_fill(3120.0, 1, FuturesOffset.CLOSE_TODAY), MULT, RATE
        )

        # 平今 = (3120−3100)×10 = +200
        self.assertAlmostEqual(acc.realized_today(D1), 200.0)
        self.assertEqual((breakdown.from_yesterday, breakdown.from_today), (0, 1))
        # 200000 − 3.1 + 200 − 3.1
        self.assertAlmostEqual(acc.balance, 200193.8)
        self.assertAlmostEqual(acc.margin_occupied, 0.0)
        self.assertAlmostEqual(acc.fees_on(D1), 2 * FEE)
        self.assertAlmostEqual(acc.fees_total(), 2 * FEE)


class ShortMirrorTest(unittest.TestCase):
    """用例 5：空头镜像——方向符号 −1 路径，数字与多头对偶。"""

    def test_short_mark_to_market_and_close(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1, direction=SHORT), MULT, RATE)
        self.assertAlmostEqual(acc.balance, 199996.9)
        self.assertAlmostEqual(acc.margin_occupied, 4030.0)

        # 结算 3050（价跌）：空单盯市 = (3050−3100)×10×(−1) = +500
        result = acc.settle(D1, {RB: 3050.0}, {RB: MULT}, RATE)
        self.assertAlmostEqual(result.position_pnl, 500.0)
        self.assertAlmostEqual(acc.balance, 200496.9)
        self.assertAlmostEqual(acc.margin_occupied, 3050.0 * 10.0 * RATE)  # 3965

        # 平昨 @3040：(3040−3050)×10×(−1) = +100
        acc.apply_fill(
            close_fill(3040.0, 1, FuturesOffset.CLOSE_YESTERDAY, direction=SHORT, trade_date=D2),
            MULT,
            RATE,
        )
        self.assertAlmostEqual(acc.realized_today(D2), 100.0)
        # 与多头镜像案例同构：全程价差 600，总账一致
        self.assertAlmostEqual(acc.balance, 200593.8)


class PlainCloseYesterdayFirstTest(unittest.TestCase):
    """用例 6：plain CLOSE 昨仓优先，一笔 fill 可跨两档并返回拆分明细。"""

    def test_close_splits_yesterday_first(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1, trade_date=D1), MULT, RATE)
        acc.settle(D1, {RB: 3150.0}, {RB: MULT}, RATE)
        acc.apply_fill(open_fill(3140.0, 1, trade_date=D2, order_id="o2"), MULT, RATE)

        # 平前可平查询：CLOSE 两档合计 2；显式口径各查各档
        for offset, expected in [
            (FuturesOffset.CLOSE, 2),
            (FuturesOffset.CLOSE_YESTERDAY, 1),
            (FuturesOffset.CLOSE_TODAY, 1),
            (FuturesOffset.OPEN, 0),
        ]:
            with self.subTest(closeable=offset.value):
                self.assertEqual(acc.closeable_lots(RB, LONG, offset, D2), expected)

        breakdown = acc.apply_fill(
            close_fill(3160.0, 2, FuturesOffset.CLOSE, trade_date=D2), MULT, RATE
        )

        # 昨档 (3160−3150)×10 = 100；今档 (3160−3140)×10 = 200（任务示例的
        # "20" 漏乘乘数，见文件头更正说明）→ 合计 300
        self.assertEqual((breakdown.from_yesterday, breakdown.from_today), (1, 1))
        self.assertAlmostEqual(acc.realized_today(D2), 300.0)
        self.assertAlmostEqual(acc.margin_occupied, 0.0)
        # 结存：200000−3.1 +500 −3.1 +300 −3.1 = 200790.7
        self.assertAlmostEqual(acc.balance, 200790.7)


class WeightedTodayCostTest(unittest.TestCase):
    """用例 7：同日多次开仓按手数加权均价。"""

    def test_weighted_today_cost(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 2), MULT, RATE)
        acc.apply_fill(open_fill(3130.0, 1, order_id="o2"), MULT, RATE)

        # (3100×2 + 3130×1) / 3 = 3110
        pos = acc.position(RB, LONG)
        self.assertEqual(pos.today_lots, 3)
        self.assertAlmostEqual(pos.today_cost, 3110.0)
        # 保证金 = 3110×10×3×0.13 = 12129（加权成本与逐笔增量一致）
        self.assertAlmostEqual(acc.margin_occupied, 12129.0)

        # 平今 1 手 @3150：(3150−3110)×10 = +400（40 是点差，须乘乘数）
        acc.apply_fill(close_fill(3150.0, 1, FuturesOffset.CLOSE_TODAY), MULT, RATE)
        self.assertAlmostEqual(acc.realized_today(D1), 400.0)
        # 剩余 2 手今仓，均价不变；释放 1 手按 3110 口径 → 余 8086
        pos = acc.position(RB, LONG)
        self.assertEqual(pos.today_lots, 2)
        self.assertAlmostEqual(pos.today_cost, 3110.0)
        self.assertAlmostEqual(acc.margin_occupied, 3110.0 * 10.0 * 2.0 * RATE)


class OverCloseRejectedTest(unittest.TestCase):
    """用例 8：超量平仓 ValueError（账户层兜底防账目穿透），账本状态不被污染。"""

    def _account_with_mixed_lots(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1, trade_date=D1), MULT, RATE)
        acc.settle(D1, {RB: 3150.0}, {RB: MULT}, RATE)   # 昨仓 1
        acc.apply_fill(open_fill(3140.0, 1, trade_date=D2, order_id="o2"), MULT, RATE)  # 今仓 1
        return acc

    def test_over_close_raises_and_keeps_ledger_intact(self):
        cases = [
            # (说明, fill, 可平手数)
            ("超昨档", close_fill(3160.0, 2, FuturesOffset.CLOSE_YESTERDAY, trade_date=D2), 1),
            ("超今档", close_fill(3160.0, 2, FuturesOffset.CLOSE_TODAY, trade_date=D2), 1),
            ("超总量", close_fill(3160.0, 3, FuturesOffset.CLOSE, trade_date=D2), 2),
        ]
        for label, fill, closeable in cases:
            with self.subTest(case=label):
                acc = self._account_with_mixed_lots()
                balance_before = acc.balance
                margin_before = acc.margin_occupied
                fills_before = len(acc.fills)
                self.assertEqual(acc.closeable_lots(RB, LONG, fill.offset, D2), closeable)

                with self.assertRaises(ValueError) as ctx:
                    acc.apply_fill(fill, MULT, RATE)
                # 错误消息含 symbol / 方向 / 可平手数，引擎可据此诊断
                msg = str(ctx.exception)
                self.assertIn(RB, msg)
                self.assertIn("LONG", msg)
                self.assertIn(f"可平 {closeable} 手", msg)
                # 拒单不污染账本
                self.assertAlmostEqual(acc.balance, balance_before)
                self.assertAlmostEqual(acc.margin_occupied, margin_before)
                self.assertEqual(len(acc.fills), fills_before)

    def test_close_without_position_raises(self):
        acc = FuturesAccount.create(INITIAL)
        with self.assertRaises(ValueError):
            acc.apply_fill(close_fill(3160.0, 1, FuturesOffset.CLOSE), MULT, RATE)


class HedgeLockTest(unittest.TestCase):
    """用例 9：锁仓——同 symbol 多空并存，各自独立计账。"""

    def test_long_and_short_coexist_independently(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1, direction=LONG), MULT, RATE)
        acc.apply_fill(open_fill(3100.0, 1, direction=SHORT, order_id="o2"), MULT, RATE)

        long_pos = acc.position(RB, LONG)
        short_pos = acc.position(RB, SHORT)
        self.assertEqual(long_pos.today_lots, 1)
        self.assertEqual(short_pos.today_lots, 1)
        # 单腿保证金口径：两条腿各自占用 4030
        self.assertAlmostEqual(acc.margin_occupied, 2 * 4030.0)
        self.assertEqual(len(acc.positions_snapshot()), 2)

        # 结算 @3120：多头 +200、空头 −200，盯市合计 0（锁仓价差冻结）
        result = acc.settle(D1, {RB: 3120.0}, {RB: MULT}, RATE)
        self.assertAlmostEqual(result.position_pnl, 0.0)
        self.assertAlmostEqual(acc.balance, 200000.0 - 2 * FEE)
        self.assertAlmostEqual(acc.margin_occupied, 2 * 3120.0 * 10.0 * RATE)

        # 各自平仓：+100 与 −100 相抵，只有费用在流血
        acc.apply_fill(
            close_fill(3130.0, 1, FuturesOffset.CLOSE_YESTERDAY, direction=LONG, trade_date=D2),
            MULT,
            RATE,
        )
        acc.apply_fill(
            close_fill(3130.0, 1, FuturesOffset.CLOSE_YESTERDAY, direction=SHORT, trade_date=D2),
            MULT,
            RATE,
        )
        self.assertAlmostEqual(acc.realized_today(D2), 0.0)  # +100 + (−100)
        self.assertAlmostEqual(acc.balance, 200000.0 - 4 * FEE)
        self.assertAlmostEqual(acc.margin_occupied, 0.0)
        self.assertEqual(acc.positions_snapshot(), [])


class SettleMissingPriceTest(unittest.TestCase):
    """用例 10：settle 缺价——errors 记 symbol、该持仓不动、balance 不变、绝不冒充。"""

    def test_missing_settle_price_skips_symbol(self):
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1, symbol=RB), MULT, RATE)
        acc.apply_fill(open_fill(3000.0, 2, symbol=HC, order_id="o2"), HC_MULT, RATE)

        # 只有 HC 有结算价，RB 缺价：即使 RB 今仓有未实现盈利也不兑现
        result = acc.settle(D1, {HC: 3050.0}, {RB: MULT, HC: HC_MULT}, RATE)

        self.assertEqual(result.errors, [RB])
        # RB 持仓与成本原样（今仓未毕业、成本未重置）
        pos = acc.position(RB, LONG)
        self.assertEqual(pos.today_lots, 1)
        self.assertAlmostEqual(pos.today_cost, 3100.0)
        # HC 正常结算：(3050−3000)×5×2 = +500
        self.assertAlmostEqual(result.position_pnl, 500.0)
        self.assertAlmostEqual(acc.balance, 200000.0 - 2 * FEE + 500.0)
        # 保证金：HC 按结算价 3050×5×2×0.13=3965；RB 保持原口径 3100×10×0.13=4030
        self.assertAlmostEqual(acc.margin_occupied, 3965.0 + 4030.0)
        self.assertAlmostEqual(result.margin_occupied_after, 7995.0)


class EquityMarksTest(unittest.TestCase):
    """用例 11：equity 盘中 marks 只补不重；无持仓/无 marks 退化为 balance。"""

    def test_equity_degrades_and_marks_up(self):
        acc = FuturesAccount.create(INITIAL)
        # 无持仓：权益=可用=结存，marks 无从加起
        self.assertAlmostEqual(acc.equity(), INITIAL)
        self.assertAlmostEqual(acc.equity({RB: 3100.0}), INITIAL)
        self.assertAlmostEqual(acc.available(), INITIAL)

        acc.apply_fill(open_fill(3100.0, 1), MULT, RATE)
        # mark 高于成本 20 点 → 权益上浮 200（balance 不含未实现）
        self.assertAlmostEqual(acc.equity({RB: 3120.0}), 199996.9 + 200.0)
        self.assertAlmostEqual(acc.available({RB: 3120.0}), 199996.9 + 200.0 - 4030.0)
        # mark 低于成本 → 权益下浮，方向对称
        self.assertAlmostEqual(acc.equity({RB: 3080.0}), 199996.9 - 200.0)
        # marks 缺该品种 → 不臆造，退回 balance 口径
        self.assertAlmostEqual(acc.equity({HC: 3000.0}), acc.balance)

        # 结算后再看 marks：成本已重置为 3150，只补 mark−3150 的部分（不重计 500）
        acc.settle(D1, {RB: 3150.0}, {RB: MULT}, RATE)
        self.assertAlmostEqual(acc.equity({RB: 3150.0}), acc.balance)
        self.assertAlmostEqual(acc.equity({RB: 3200.0}), 200496.9 + 500.0)


class ValidationTest(unittest.TestCase):
    """钉死接口的输入校验与边角查询。"""

    def test_non_positive_lots_or_price_rejected(self):
        acc = FuturesAccount.create(INITIAL)
        for label, fill in [
            ("lots=0", open_fill(3100.0, 0)),
            ("lots<0", open_fill(3100.0, -1)),
            ("price=0", open_fill(0.0, 1)),
            ("price<0", open_fill(-3100.0, 1)),
        ]:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    acc.apply_fill(fill, MULT, RATE)
        self.assertEqual(acc.fills, [])

    def test_queries_on_unknown_date_and_symbol(self):
        acc = FuturesAccount.create(INITIAL)
        # 未交易日期/品种：平仓盈亏与可平均为 0，position 为 None
        self.assertAlmostEqual(acc.realized_today("19990101"), 0.0)
        self.assertAlmostEqual(acc.fees_on("19990101"), 0.0)
        self.assertAlmostEqual(acc.fees_total(), 0.0)
        self.assertIsNone(acc.position(RB, LONG))
        self.assertEqual(acc.closeable_lots(RB, LONG, FuturesOffset.CLOSE, D1), 0)

    def test_fill_and_result_records_kept(self):
        # 审计留痕：fills / settlements 全量可追溯
        acc = FuturesAccount.create(INITIAL)
        fill = open_fill(3100.0, 1, order_id="audit-1")
        acc.apply_fill(fill, MULT, RATE)
        acc.settle(D1, {RB: 3150.0}, {RB: MULT}, RATE)
        self.assertEqual(acc.fills, [fill])
        self.assertEqual(len(acc.settlements), 1)
        self.assertEqual(acc.settlements[0].trade_date, D1)

    def test_position_returns_defensive_copy(self):
        # 返回的是快照拷贝：外部改不动账本内部状态
        acc = FuturesAccount.create(INITIAL)
        acc.apply_fill(open_fill(3100.0, 1), MULT, RATE)
        snap = acc.position(RB, LONG)
        snap.today_lots = 99
        self.assertEqual(acc.position(RB, LONG).today_lots, 1)
        snap2 = acc.positions_snapshot()[0]
        snap2.today_lots = 99
        self.assertEqual(acc.position(RB, LONG).today_lots, 1)

    def test_dataclass_shapes(self):
        # 接口形状钉死：默认值可空构造，供撮合/存储层按字段名对接
        self.assertEqual((CloseBreakdown().from_yesterday, CloseBreakdown().from_today), (0, 0))
        self.assertEqual(
            (PositionState(RB, LONG).yesterday_lots, PositionState(RB, LONG).today_lots), (0, 0)
        )


if __name__ == "__main__":
    unittest.main()
