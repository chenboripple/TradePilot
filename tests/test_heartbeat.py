"""心跳参数巡检测试（P6 迁移后：统一引擎口径、离线可测）。

覆盖四件事：

1. ``run_once`` 闭环离线可跑（注入 fetch + tmp 路径），state/runs 落盘且
   结果 dict 保留旧 state schema 键名（存量 state 兼容）；
2. 撮合走统一引擎——卖出成交费率含印花税（≈ 买入费率 + 0.0005）；
3. 涨停开盘无法买入（信号次日一字涨停 → skipped_fills 记原因、当日无成交）；
4. CLI ``tradepilot heartbeat`` 的再拟合闸门：默认停用只提示，--allow-refit
   /TRADEPILOT_AUTOFIT=1 才放行。
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.rules import MarketRules, price_limit_for_symbol
from ripple_tradePilot.cli import cli
from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.monitor import heartbeat as hb
import synth

SYMBOL = "600309.SH"  # 沪主板：±10% 涨跌停
PARAMS = hb.Params(ma_fast=3, ma_slow=10, rsi_period=14, rsi_oversold=30,
                   rsi_overbought=70, bb_period=20, bb_std=2.0)
# 旧 state 文件里的一条存量记录（键名/结构与迁移前完全一致）
LEGACY_PARAMS = {"ma_fast": 5, "ma_slow": 20, "rsi_period": 14,
                 "rsi_oversold": 30, "rsi_overbought": 70,
                 "bb_period": 20, "bb_std": 2.0}


def _bars(count: int = 160) -> list:
    """正弦震荡：周期性触发 MA 金叉/死叉、RSI 超买超卖与布林带触轨。"""
    return synth.bars_from_closes(synth.sine_closes(count, period=20.0))


def _engine_backtest(bars, params=PARAMS, **overrides):
    kwargs = dict(
        initial_cash=100000.0,
        execution="next_open",
        market_rules=MarketRules(price_limit_pct=price_limit_for_symbol(SYMBOL)),
    )
    kwargs.update(overrides)
    return run_backtest(hb.build_strategy(params), bars, **kwargs)


class TestRunOnce:
    def test_writes_state_and_runs_record(self, tmp_path):
        state_path = tmp_path / "heartbeat_strategy_state.json"
        runs_dir = tmp_path / "runs"

        payload = hb.run_once(
            targets=[(SYMBOL, "万华化学")],
            fetch=lambda symbol, days: _bars(),
            state_path=state_path,
            runs_dir=runs_dir,
        )

        assert len(payload["results"]) == 1
        item = payload["results"][0]
        assert item["decision"] in {"promote-candidate", "keep-baseline"}
        # 网格含缺省参数 → 候选至少不劣于 baseline
        assert (item["candidate_result"]["score"]
                >= item["baseline_result"]["score"] - 1e-9)
        # 结果 dict 保留旧 state schema 键名（total_return/win_rate/max_drawdown 为百分数）
        for key in ("initial_capital", "final_capital", "total_return",
                    "total_trades", "win_rate", "max_drawdown", "score", "trades"):
            assert key in item["active_result"], key

        state = json.loads(state_path.read_text(encoding="utf-8"))
        entry = state["symbols"][SYMBOL]
        assert entry["active_params"] == item["active_params"]
        assert entry["history"] and entry["history"][-1]["decision"] == item["decision"]
        assert state["updated_at"]
        assert list(runs_dir.glob("heartbeat_run_*.json"))

    def test_reads_legacy_state(self, tmp_path):
        """存量 state（迁移前脚本写的）直接续用：baseline 取旧参数、history 追加。"""
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({
            "updated_at": "2025-01-01 00:00:00",
            "symbols": {SYMBOL: {"active_params": LEGACY_PARAMS,
                                 "last_best_result": None,
                                 "history": []}},
        }, ensure_ascii=False), encoding="utf-8")

        payload = hb.run_once(
            targets=[(SYMBOL, "万华化学")],
            fetch=lambda symbol, days: _bars(),
            state_path=state_path,
            runs_dir=tmp_path / "runs",
        )

        item = payload["results"][0]
        assert item["baseline_params"] == LEGACY_PARAMS
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(state["symbols"][SYMBOL]["history"]) == 1

    def test_data_insufficient_symbol_recorded_not_raised(self, tmp_path):
        """数据不足的标的记 data-insufficient，不中断、也写 state。"""
        state_path = tmp_path / "state.json"

        payload = hb.run_once(
            targets=[("600309.SH", "万华化学"), ("002022.SZ", "科华生物")],
            fetch=lambda symbol, days: _bars() if symbol == "600309.SH" else [],
            state_path=state_path,
            runs_dir=tmp_path / "runs",
        )

        by_symbol = {item["symbol"]: item for item in payload["results"]}
        assert by_symbol["002022.SZ"]["decision"] == "data-insufficient"
        assert by_symbol["600309.SH"]["decision"] in {"promote-candidate", "keep-baseline"}
        assert state_path.exists()


class TestUnifiedEngineSemantics:
    def test_sell_fee_includes_stamp_duty(self):
        """统一引擎口径：卖出费率 = 佣金 + 印花税 0.0005（买入只有佣金）。"""
        result = _engine_backtest(_bars())
        buys = [f for f in result.fills if f.side == Side.BUY]
        sells = [f for f in result.fills if f.side == Side.SELL]
        assert buys, "合成序列应至少产生一笔买入"
        assert sells, "合成序列应至少产生一笔卖出"

        buy_rates = [f.fee / (f.price * f.quantity) for f in buys]
        sell_rates = [f.fee / (f.price * f.quantity) for f in sells]
        gap = min(sell_rates) - max(buy_rates)
        assert abs(gap - 0.0005) < 2e-4, f"卖出应比买入多 0.0005 印花税，实差 {gap:.6f}"

    def test_buy_skipped_when_next_open_at_limit_up(self):
        """信号次日一字涨停开盘 → 涨停无法买入（skip 记原因、当日无买单成交）。"""
        bars = _bars()
        # 找第一根 BUY 信号 bar：只依赖 ≤i 的数据，改 i+1 不影响它
        strategy = hb.build_strategy(PARAMS)
        signal_index = None
        for index, bar in enumerate(bars):
            if strategy.on_bar(bar).side == Side.BUY:
                signal_index = index
                break
        assert signal_index is not None, "合成序列应产生 BUY 信号"
        assert signal_index + 1 < len(bars)

        prev_close = bars[signal_index].close
        limit_open = round(prev_close * 1.10, 4)  # 沪主板涨停价
        next_bar = bars[signal_index + 1]
        bars[signal_index + 1] = Bar(
            timestamp=next_bar.timestamp,
            open=limit_open,
            high=limit_open,   # 一字板：全天锁死
            low=prev_close,
            close=limit_open,
            volume=next_bar.volume,
        )

        result = _engine_backtest(bars)
        assert any(skip["reason"] == "涨停无法买入" for skip in result.skipped_fills)
        blocked_day = bars[signal_index + 1].timestamp
        assert all(fill.timestamp != blocked_day or fill.side != Side.BUY
                   for fill in result.fills)

    def test_result_dict_matches_engine_totals(self):
        """run_symbol_backtest 的汇总与直接调引擎一致（口径不偷换）。"""
        bars = _bars(120)
        summary = hb.run_symbol_backtest(SYMBOL, bars, PARAMS)
        engine = _engine_backtest(bars)
        assert summary["final_capital"] == engine.equity_curve[-1]
        assert summary["skipped_fills"] == len(engine.skipped_fills)


class TestHeartbeatCli:
    def test_refit_disabled_by_default(self, monkeypatch):
        """默认停用：只打印过拟合警示，不触网不落盘。"""
        monkeypatch.delenv("TRADEPILOT_AUTOFIT", raising=False)
        result = CliRunner().invoke(cli, ["heartbeat"])
        assert result.exit_code == 0
        assert "默认停用" in result.output

    def test_allow_refit_runs_pipeline(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRADEPILOT_DATA_DIR", str(tmp_path))  # 摘要/状态都进 tmp
        # 配置与通知全部打桩：不读用户真实 config.yaml，更不允许真发飞书
        monkeypatch.setattr("ripple_tradePilot.cli.load_config", lambda *a, **k: {})
        monkeypatch.setattr(hb, "maybe_notify", lambda config, payload: None)
        called = {}

        def fake_run_once(**kwargs):
            called.update(kwargs)
            return {"run_at": "2026-10-06 12:00:00", "mode": "unified-engine",
                    "results": [{
                        "symbol": "600309.SH", "name": "万华化学",
                        "decision": "keep-baseline",
                        "baseline_params_label": "b", "baseline_result": {"total_return": 1.0},
                        "candidate_params_label": "c", "candidate_result": {"total_return": 2.0},
                        "active_params_label": "MA3/10, RSI14/30/70, BB20/2.0",
                        "active_result": {"total_return": 1.0, "max_drawdown": 5.0,
                                          "win_rate": 50.0, "total_trades": 3},
                    }]}

        monkeypatch.setattr(hb, "run_once", fake_run_once)
        result = CliRunner().invoke(cli, ["heartbeat", "--allow-refit", "--days", "300"])
        assert result.exit_code == 0, result.output
        assert called["days"] == 300
        assert called["targets"]
        assert "keep-baseline" in result.output
        # 摘要落到 TRADEPILOT_DATA_DIR 指定的 tmp 目录
        assert (tmp_path / "backtest" / "OPTIMIZATION_SUMMARY.md").exists()
