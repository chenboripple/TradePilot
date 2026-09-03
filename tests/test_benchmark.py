"""
基准对比单元测试

全部使用合成数据，不联网、不使用真实 Tushare token：
- compare_with_benchmark 的数值语义（超额收益、长度对齐、空输入、
  回撤改善、相关系数与 beta）
- get_index_bars 用伪造的 pro 接口测试：参数透传、日期升序排序、
  异常/空结果时返回空 DataFrame
"""

import unittest

import numpy as np
import pandas as pd

from ripple_tradePilot.backtest.report import (
    BenchmarkComparison,
    compare_with_benchmark,
)
from ripple_tradePilot.data.tushare_loader import TushareDataLoader


class CompareWithBenchmarkTest(unittest.TestCase):
    def test_strategy_doubles_benchmark_flat(self):
        """策略翻倍、基准不动 → 超额收益 100%。"""
        result = compare_with_benchmark(
            [100.0, 150.0, 200.0], [100.0, 100.0, 100.0]
        )
        self.assertIsInstance(result, BenchmarkComparison)
        self.assertAlmostEqual(result.strategy_return, 1.0)
        self.assertAlmostEqual(result.benchmark_return, 0.0)
        self.assertAlmostEqual(result.excess_return, 1.0)
        self.assertAlmostEqual(result.strategy_max_drawdown, 0.0)
        self.assertAlmostEqual(result.benchmark_max_drawdown, 0.0)
        self.assertAlmostEqual(result.drawdown_improvement, 0.0)
        # 基准收益恒为 0（方差为 0），相关系数与 beta 无法计算，置 0
        self.assertEqual(result.correlation, 0.0)
        self.assertEqual(result.beta, 0.0)

    def test_length_mismatch_aligns_to_shorter(self):
        """长度不一致时按较短一方截断对齐。"""
        # 策略较长：若不对齐，尾部下跌会把总收益拉到 -0.5
        equity = [100.0, 110.0, 121.0, 50.0, 50.0]
        benchmark = [100.0, 110.0, 121.0]
        result = compare_with_benchmark(equity, benchmark)
        self.assertAlmostEqual(result.strategy_return, 0.21)
        self.assertAlmostEqual(result.benchmark_return, 0.21)
        self.assertAlmostEqual(result.excess_return, 0.0)
        self.assertAlmostEqual(result.strategy_max_drawdown, 0.0)

        # 基准较长：同理按策略长度截断
        result2 = compare_with_benchmark(
            [100.0, 110.0, 121.0], [100.0, 110.0, 121.0, 60.0]
        )
        self.assertAlmostEqual(result2.strategy_return, 0.21)
        self.assertAlmostEqual(result2.benchmark_return, 0.21)

    def test_empty_input_raises(self):
        """任一输入为空抛 ValueError。"""
        with self.assertRaises(ValueError):
            compare_with_benchmark([], [])
        with self.assertRaises(ValueError):
            compare_with_benchmark([], [100.0, 110.0])
        with self.assertRaises(ValueError):
            compare_with_benchmark([100.0, 110.0], [])

    def test_single_point_returns_zero(self):
        """对齐后长度不足 2，无法计算收益，返回全零。"""
        result = compare_with_benchmark([100.0], [50.0])
        self.assertEqual(result.strategy_return, 0.0)
        self.assertEqual(result.benchmark_return, 0.0)
        self.assertEqual(result.excess_return, 0.0)

    def test_drawdown_improvement(self):
        """策略回撤 -10%，基准回撤 -30% → 回撤改善 +0.2。"""
        result = compare_with_benchmark(
            [100.0, 90.0, 95.0], [100.0, 70.0, 80.0]
        )
        self.assertAlmostEqual(result.strategy_max_drawdown, -0.10)
        self.assertAlmostEqual(result.benchmark_max_drawdown, -0.30)
        self.assertAlmostEqual(result.drawdown_improvement, 0.20)
        self.assertAlmostEqual(result.strategy_return, -0.05)
        self.assertAlmostEqual(result.benchmark_return, -0.20)
        self.assertAlmostEqual(result.excess_return, 0.15)

    def test_correlation_and_beta(self):
        """策略日收益恒为基准 2 倍 → 相关系数 1、beta 2。"""
        benchmark = [100.0, 102.0, 101.0, 105.0, 103.0, 108.0]
        b_ret = np.diff(benchmark) / np.asarray(benchmark[:-1], dtype=float)
        equity = [100.0]
        for r in b_ret:
            equity.append(equity[-1] * (1.0 + 2.0 * float(r)))
        result = compare_with_benchmark(equity, benchmark)
        self.assertAlmostEqual(result.correlation, 1.0, places=8)
        self.assertAlmostEqual(result.beta, 2.0, places=8)


class FakeProApi:
    """伪造 Tushare pro 接口，仅拦截 index_daily 调用。"""

    def __init__(self, df=None, error=None):
        self.df = df
        self.error = error
        self.calls = []

    def index_daily(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.df


def make_loader(pro) -> TushareDataLoader:
    """绕过 __init__（内部会调用 ts.set_token/pro_api），避免网络与 token 副作用。"""
    loader = object.__new__(TushareDataLoader)
    loader.pro = pro
    loader._rate_limit_delay = 0.0
    loader._last_request_time = 0.0
    return loader


class GetIndexBarsTest(unittest.TestCase):
    def test_returns_ascending_bars_and_passes_params(self):
        df = pd.DataFrame(
            {
                'ts_code': ['000300.SH'] * 3,
                'trade_date': ['20260102', '20260101', '20260103'],
                'close': [101.0, 100.0, 102.0],
            }
        )
        pro = FakeProApi(df=df)
        loader = make_loader(pro)
        result = loader.get_index_bars(
            start_date='20260101', end_date='20260103'
        )
        self.assertEqual(len(result), 3)
        self.assertEqual(
            list(result['trade_date']), ['20260101', '20260102', '20260103']
        )
        self.assertEqual(pro.calls[0]['ts_code'], '000300.SH')
        self.assertEqual(pro.calls[0]['start_date'], '20260101')
        self.assertEqual(pro.calls[0]['end_date'], '20260103')
        # 确认走过限流（_rate_limit 会刷新最近请求时间）
        self.assertGreater(loader._last_request_time, 0.0)

    def test_api_error_returns_empty_dataframe(self):
        loader = make_loader(FakeProApi(error=RuntimeError('network down')))
        result = loader.get_index_bars(
            start_date='20260101', end_date='20260103'
        )
        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)

    def test_empty_result_returns_empty_dataframe(self):
        loader = make_loader(FakeProApi(df=pd.DataFrame()))
        result = loader.get_index_bars()  # 默认参数：沪深300，最近一年
        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)
        self.assertEqual(loader.pro.calls[0]['ts_code'], '000300.SH')


if __name__ == '__main__':
    unittest.main()
