"""期货回测入口层测试（roadmap P2「能重现报告」+ 入口验收）。

夹具模拟 futures scan 落库后的形态：60m bar（bar_time=结束时刻）+ 日线
（settle/volume/hold）+ 报价快照（费率/保证金）。期望值不手算全账（引擎
与 walkforward 已各有黄金用例），这里钉的是入口层职责：取数、规则推导
（快照优先/规格兜底/近似标记）、落库可重放、无数据时明确报错。
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from click.testing import CliRunner

from ripple_tradePilot.backtest.futures_engine import EngineConfig
from ripple_tradePilot.backtest.futures_runner import (
    FuturesDataUnavailableError,
    derive_schedules,
    load_product_contracts,
    run_product_backtest,
    run_product_walkforward,
)
from ripple_tradePilot.cli import cli
from ripple_tradePilot.storage.database import (
    load_futures_account_daily,
    load_futures_orders,
    load_futures_trades,
    upsert_futures_bars,
    upsert_futures_quotes,
)
from ripple_tradePilot.storage.user_store import list_backtest_runs

OLD, NEW = "RB2610.SHFE", "RB2701.SHFE"
_START = date(2026, 3, 2)
DAYS = 120


def date_of(i: int) -> str:
    # 连续真实日历日：规则层硬校验 8 位真实日期，且字典序 == 时间序
    return (_START + timedelta(days=i)).strftime("%Y%m%d")


def seeded_bars():
    """阶梯上涨（每天 +1 点，每日 2 根 60m，无回抽）：首个可评估 bar 即 LONG 后保持。

    high/low 不加 ±0.5 摆动：日内回抽会触发出场通道，倾向在 LONG/NEUTRAL
    间每 bar 闪烁——入口层要测的是「信号→持仓→换月」主链路，不是震荡语义。
    """
    rows = []
    for i in range(DAYS):
        for minute, open_, close_ in ((10, 100 + i, 100.2 + i),
                                      (11, 100.2 + i, 100.5 + i)):
            rows.append({
                "symbol": OLD, "trade_date": date_of(i),
                "bar_time": f"{date_of(i)} {minute}:00:00",
                "open": open_, "high": max(open_, close_),
                "low": min(open_, close_), "close": close_,
                "volume": 50000, "hold": 200000, "settle": None,
                "source": "sina",
            })
    return rows


def seeded_daily(symbol, dates_shift=0):
    return [{"symbol": symbol, "trade_date": date_of(i + dates_shift),
             "bar_time": "", "open": 100 + i, "high": 101 + i, "low": 99 + i,
             "close": 100.5 + i, "volume": 50000 + i * 10, "hold": 200000,
             "settle": 100.5 + i, "source": "sina"}
            for i in range(DAYS)]


def seed(path: Path, *, with_quotes=True, with_new_contract=False):
    upsert_futures_bars("60m", seeded_bars(), path)
    upsert_futures_bars("1d", seeded_daily(OLD), path)
    if with_new_contract:
        # 新主力：60 天前成交量远小于旧合约（否则 tiebreak 按到期远近直接选新），
        # 第 61 天起碾压 → 主力切换一次（换月路径）
        upsert_futures_bars("60m", [
            {**row, "symbol": NEW} for row in seeded_bars()[120:]
        ], path)
        upsert_futures_bars("1d", [
            {**row, "symbol": NEW,
             "volume": (8000 + i) if i < 60 else 900000}
            for i, row in enumerate(seeded_daily(NEW))
        ], path)
    if with_quotes:
        # P0 核验口径：rb 每手保证金 2170 ÷ (3100×10) = 7%
        upsert_futures_quotes([{
            "symbol": OLD, "price": 3100.0, "upper_limit": 3317.0,
            "lower_limit": 2883.0, "margin_per_hand": 2170.0,
            "margin_is_estimate": 1, "fee_per_lot": 3.6, "is_main": 1,
            "price_time": f"{date_of(DAYS - 1)} 15:00:00", "source": "sina",
        }], path)


class LoadProductContractsTest(unittest.TestCase):
    """取数职责：只收本品种、60m 全空明确报错指路 scan。"""

    def test_loads_only_product_contracts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runner.db"
            seed(path, with_new_contract=True)
            contracts = load_product_contracts("RB", path)
            self.assertEqual(sorted(contracts), sorted([OLD, NEW]))
            for data in contracts.values():
                self.assertEqual(data.multiplier, 10.0)   # RB 规格
                self.assertEqual(data.tick_size, 1.0)
                self.assertTrue(data.daily)

    def test_unknown_product_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runner.db"
            seed(path)
            with self.assertRaises(ValueError):
                load_product_contracts("TA", path)

    def test_no_data_points_to_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "empty.db"
            with self.assertRaises(FuturesDataUnavailableError) as ctx:
                load_product_contracts("RB", path)
            self.assertIn("futures scan", str(ctx.exception))


class DeriveSchedulesTest(unittest.TestCase):
    """规则推导：快照优先、规格兜底、一律近似口径。"""

    def _contracts(self, path):
        seed(path)
        return load_product_contracts("RB", path)

    def test_quote_snapshot_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "q.db"
            contracts = self._contracts(path)
            fees, margins, notes = derive_schedules("RB", contracts, path)
            rule = fees.rule_on(date_of(0))
            # 快照 fee_per_lot=3.6 → 三档同价；保证金 1540/(220×10) = 7%
            self.assertEqual(rule.open_per_lot, 3.6)
            self.assertEqual(rule.close_today_per_lot, 3.6)
            self.assertAlmostEqual(margins.rate_on(date_of(0)), 0.07)
            self.assertTrue(rule.approximate)
            self.assertTrue(margins.is_approximate_on(date_of(0)))
            self.assertTrue(any("近似" in note for note in notes))

    def test_spec_fallback_without_quotes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "noq.db"
            seed(path, with_quotes=False)
            contracts = load_product_contracts("RB", path)
            fees, margins, _ = derive_schedules("RB", contracts, path)
            rule = fees.rule_on(date_of(0))
            # 规格：往返 6.2 → 单边 3.1；保证金率 0.07
            self.assertAlmostEqual(rule.open_per_lot, 3.1)
            self.assertAlmostEqual(margins.rate_on(date_of(0)), 0.07)


class RunProductBacktestTest(unittest.TestCase):
    """单次回测：跑通 + 审计三表按 run_id 可重放 + backtest_results 汇总行。"""

    def test_runs_and_persists_full_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run.db"
            seed(path, with_new_contract=True)
            report, notes = run_product_backtest(
                "RB", entry_window=10, config=EngineConfig(run_id="rb-run-1"),
                path=path)
            self.assertTrue(any("近似" in note for note in notes))
            self.assertGreater(report.metrics["fills"], 0)
            self.assertEqual(report.metrics["rolls"], 1)   # 60 天后主力切换
            with self.subTest(step="审计三表按 run_id 重放"):
                orders = load_futures_orders("rb-run-1", path)
                trades = load_futures_trades("rb-run-1", path)
                daily = load_futures_account_daily("rb-run-1", path)
                self.assertEqual(len(orders), len(report.orders))
                self.assertEqual(len(trades), len(report.trades))
                self.assertEqual([row["trade_date"] for row in daily],
                                 [row["trade_date"] for row in report.daily])
            with self.subTest(step="汇总行落库"):
                runs = list_backtest_runs(kind="futures_backtest", path=path)
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["symbol"], "RB")
                self.assertAlmostEqual(runs[0]["total_return"],
                                       report.metrics["total_return"])
                params = json.loads(runs[0]["params_json"])
                self.assertEqual(params["entry_window"], 10)
            with self.subTest(step="权重不变式：结清后权益=结存"):
                self.assertEqual(daily[-1]["margin_occupied"], 0.0)
                self.assertEqual(daily[-1]["equity"], daily[-1]["balance"])

    def test_no_save_leaves_no_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nosave.db"
            seed(path)
            report, _ = run_product_backtest(
                "RB", entry_window=10,
                config=EngineConfig(run_id="rb-nosave"), path=path, save=False)
            self.assertEqual(load_futures_orders("rb-nosave", path), [])
            self.assertEqual(list_backtest_runs(path=path), [])


class RunProductWalkforwardTest(unittest.TestCase):
    """滚动样本外：manifest 落库、保留集存在、汇总行 run_kind 区分。"""

    def test_walkforward_persists_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wf.db"
            seed(path)
            report, _ = run_product_walkforward("RB", path=path)
            self.assertIsNotNone(report.holdout)
            self.assertEqual(len(report.segments), 4)   # 3 测试块 + 保留集
            runs = list_backtest_runs(kind="futures_backtest_walkforward",
                                      path=path)
            self.assertEqual(len(runs), 1)
            # list 视图不带大字段，manifest 从行里直读
            with sqlite3.connect(path) as connection:
                report_json = connection.execute(
                    "SELECT report_json FROM backtest_results "
                    "WHERE run_kind = 'futures_backtest_walkforward'"
                ).fetchone()[0]
            manifest = json.loads(report_json)["manifest"]
            self.assertEqual(manifest["holdout_start"], report.holdout.test_start)
            self.assertTrue(manifest["fee_approximate"])   # 快照口径必须透出
            self.assertIn("donchian-grid", manifest["strategy_version"])


class FuturesBacktestCliTest(unittest.TestCase):
    """CLI 入口：数据缺失指路 scan；有数据时出报告并落库。"""

    def _invoke(self, path, args):
        import os
        from unittest.mock import patch
        runner = CliRunner()
        with patch.dict(os.environ, {"TRADEPILOT_BACKTEST_DB": str(path)}):
            return runner.invoke(cli, args, catch_exceptions=False)

    def test_no_data_exits_with_scan_hint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cli.db"
            result = self._invoke(path, ["futures", "backtest", "-p", "RB"])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("futures scan", result.output)

    def test_cli_runs_and_saves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cli2.db"
            seed(path)
            result = self._invoke(
                path, ["futures", "backtest", "-p", "RB",
                       "--entry-window", "10", "--lots", "1"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("期末权益", result.output)
            self.assertIn("近似口径", result.output)
            with sqlite3.connect(path) as connection:
                kinds = {row[0] for row in connection.execute(
                    "SELECT DISTINCT run_kind FROM backtest_results")}
            self.assertIn("futures_backtest", kinds)

    def test_cli_walkforward_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cli3.db"
            seed(path)
            result = self._invoke(path, ["futures", "backtest", "-p", "RB",
                                         "--walkforward"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("滚动样本外验证", result.output)
            self.assertIn("保留集", result.output)


if __name__ == "__main__":
    unittest.main()
