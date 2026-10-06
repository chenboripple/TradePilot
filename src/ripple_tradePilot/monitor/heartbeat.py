"""心跳参数巡检（自根目录 heartbeat_tradepilot.py 迁入统一引擎，P6）。

闭环：读当前生效参数（state）作 baseline → 网格搜索候选 → 统一撮合引擎
（next_open / T+1 / 涨跌停 / 佣金+印花税+滑点全生效）回测对比 → 优于
baseline 才写入 state，否则回退 → 落 JSON 运行记录 + 摘要 + 可选飞书通知。

与旧脚本（已删除）的差异：

- **撮合**：旧脚本信号当日收盘价成交、只收万三佣金、无 T+1/涨跌停拦截；
  现走 :func:`ripple_tradePilot.backtest.engine.run_backtest` 统一引擎，
  结论口径与 Web「回测记录」/``tradepilot backtest`` 完全一致；
- **数据**：旧脚本自带 CSV 缓存层；现走
  :func:`ripple_tradePilot.data.stock_service.load_symbol_bars_db_first`
  （DB 优先 + refresh 补拉 + 复权审计），不再维护第二套缓存；
- **状态兼容**：``data/backtest/heartbeat_strategy_state.json`` 路径与
  schema 不变，存量 state 直接续用；``result`` dict 保留旧键名
  （total_return/win_rate/max_drawdown 均为百分数值，score 公式不变），
  但数值口径变为统一引擎口径——与旧记录不可直接对比。

过拟合防护（沿用旧脚本的闸门）：网格再拟合默认停用，必须显式
``--allow-refit`` 或 ``TRADEPILOT_AUTOFIT=1`` 才执行。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import compute_metrics, pair_trades
from ripple_tradePilot.backtest.rules import MarketRules, price_limit_for_symbol
from ripple_tradePilot.data.stock_service import load_symbol_bars_db_first
from ripple_tradePilot.models.types import Bar
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.strategies.combo_vote import ComboVoteStrategy
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI

# 兼容旧脚本默认标的（config.symbols 未配置时的兜底）
DEFAULT_TARGETS: Tuple[Tuple[str, str], ...] = (
    ("002022.SZ", "科华生物"),
    ("600309.SH", "万华化学"),
)

INITIAL_CASH = 100000.0


def heartbeat_dirs(data_dir: Optional[Path] = None) -> Tuple[Path, Path, Path]:
    """(state 路径, runs 目录, 摘要 MD 路径)——沿用旧脚本在仓库根的相对布局。"""
    base = data_dir or Path(os.getenv("TRADEPILOT_DATA_DIR", Path.cwd() / "data"))
    state = base / "backtest" / "heartbeat_strategy_state.json"
    runs = base / "backtest" / "heartbeat_runs"
    summary = base / "backtest" / "OPTIMIZATION_SUMMARY.md"
    return state, runs, summary


@dataclass
class Params:
    ma_fast: int
    ma_slow: int
    rsi_period: int
    rsi_oversold: float
    rsi_overbought: float
    bb_period: int
    bb_std: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Params":
        return cls(**data)

    def label(self) -> str:
        return (
            f"MA{self.ma_fast}/{self.ma_slow}, "
            f"RSI{self.rsi_period}/{self.rsi_oversold}/{self.rsi_overbought}, "
            f"BB{self.bb_period}/{self.bb_std}"
        )


# 与旧脚本同一网格（含义已变：候选参数现按统一引擎口径评估）
DEFAULT_GRID = {
    "ma": [(3, 10), (3, 12), (5, 15), (5, 20), (8, 21), (10, 30)],
    "rsi": [(10, 35, 65), (12, 30, 70), (14, 30, 70), (14, 35, 65)],
    "bb": [(14, 1.8), (20, 1.5), (20, 2.0), (26, 2.0)],
}


def build_strategy(params: Params) -> ComboVoteStrategy:
    """旧脚本的「任一看多且无看空 → BUY」投票，由 ComboVoteStrategy(threshold=1)
    逐字实现（buy≥1 且 sell=0 → BUY；sell≥1 且 buy=0 → SELL）。"""
    return ComboVoteStrategy(
        [
            ("ma", MovingAverageCross(fast=params.ma_fast, slow=params.ma_slow)),
            ("rsi", RSI(period=params.rsi_period,
                        oversold=params.rsi_oversold,
                        overbought=params.rsi_overbought)),
            ("bb", BollingerBands(period=params.bb_period, std_dev=params.bb_std)),
        ],
        vote_threshold=1,
    )


def run_symbol_backtest(
    symbol: str,
    bars: Sequence[Bar],
    params: Params,
    initial_cash: float = INITIAL_CASH,
) -> dict:
    """统一引擎跑一组参数，返回与旧 state schema 同键名的结果 dict。

    口径变化（相对旧脚本）：next_open 撮合（信号次日开盘）、佣金+印花税+
    滑点、T+1、按标的分板块涨跌停拦截。total_return/win_rate/max_drawdown
    为百分数值；score 公式沿用旧脚本（收益 − 0.35×回撤 + 0.02×胜率）。
    """
    result = run_backtest(
        build_strategy(params),
        bars,
        initial_cash=initial_cash,
        execution="next_open",
        market_rules=MarketRules(price_limit_pct=price_limit_for_symbol(symbol)),
    )
    trades = [
        {
            "entry_time": t["entry_time"].strftime("%Y-%m-%d"),
            "exit_time": t["exit_time"].strftime("%Y-%m-%d"),
            "return": t["return"],
        }
        for t in pair_trades(result.fills)
    ]
    metrics = compute_metrics(result.equity_curve, positions=result.positions)
    total_return = metrics.total_return * 100
    max_drawdown = -metrics.max_drawdown * 100  # 旧脚本回撤为正数口径
    win_rate = 100.0 * sum(1 for t in trades if t["return"] > 0) / len(trades) if trades else 0.0
    score = total_return - max_drawdown * 0.35 + win_rate * 0.02
    return {
        "initial_capital": initial_cash,
        "final_capital": result.equity_curve[-1] if result.equity_curve else initial_cash,
        "total_return": total_return,
        "total_trades": len(trades),
        "win_rate": win_rate,
        "max_drawdown": max_drawdown,
        "score": score,
        "trades": trades,
        "skipped_fills": len(result.skipped_fills),
    }


def fetch_bars(symbol: str, days: int = 365) -> List[Bar]:
    """DB 优先取数（替代旧脚本的 CSV 缓存层）。"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    return load_symbol_bars_db_first(symbol, start, end, initial_days=max(days, 365))


def _default_params(config: dict) -> dict:
    """缺省参数链：config.strategies 各策略 params → 旧脚本缺省值。"""
    default = config.get("strategies", {}) if config else {}
    return {
        "ma_fast": default.get("ma_cross", {}).get("params", {}).get("fast", 5),
        "ma_slow": default.get("ma_cross", {}).get("params", {}).get("slow", 20),
        "rsi_period": default.get("rsi", {}).get("params", {}).get("period", 14),
        "rsi_oversold": default.get("rsi", {}).get("params", {}).get("oversold", 30),
        "rsi_overbought": default.get("rsi", {}).get("params", {}).get("overbought", 70),
        "bb_period": default.get("bollinger", {}).get("params", {}).get("period", 20),
        "bb_std": default.get("bollinger", {}).get("params", {}).get("std_dev", 2.0),
    }


def load_state(state_path: Path, config: dict) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {
        "updated_at": None,
        "symbols": {
            code: {
                "active_params": _default_params(config),
                "last_best_result": None,
                "history": [],
            }
            for code, _ in DEFAULT_TARGETS
        },
    }


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def optimize_for_symbol(
    symbol: str, bars: Sequence[Bar]
) -> Tuple[Params, dict]:
    results = []
    for ma_fast, ma_slow in DEFAULT_GRID["ma"]:
        for rsi_period, rsi_oversold, rsi_overbought in DEFAULT_GRID["rsi"]:
            for bb_period, bb_std in DEFAULT_GRID["bb"]:
                params = Params(
                    ma_fast=ma_fast,
                    ma_slow=ma_slow,
                    rsi_period=rsi_period,
                    rsi_oversold=rsi_oversold,
                    rsi_overbought=rsi_overbought,
                    bb_period=bb_period,
                    bb_std=bb_std,
                )
                results.append((params, run_symbol_backtest(symbol, bars, params)))
    results.sort(
        key=lambda item: (
            item[1]["score"],
            item[1]["total_return"],
            item[1]["win_rate"],
            -item[1]["max_drawdown"],
        ),
        reverse=True,
    )
    return results[0]


def better(candidate: dict, baseline: dict) -> bool:
    if candidate["score"] > baseline["score"] + 1e-9:
        return True
    if abs(candidate["score"] - baseline["score"]) <= 1e-9:
        if candidate["total_return"] > baseline["total_return"] + 1e-9:
            return True
        if abs(candidate["total_return"] - baseline["total_return"]) <= 1e-9:
            return candidate["max_drawdown"] < baseline["max_drawdown"]
    return False


def run_once(
    *,
    targets: Optional[Sequence[Tuple[str, str]]] = None,
    days: int = 365,
    fetch: Optional[Callable[[str, int], Sequence[Bar]]] = None,
    state_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
    config: Optional[dict] = None,
) -> dict:
    """跑一轮「baseline → 网格 → 提升才写回」闭环，返回运行记录 payload。

    ``fetch``/``state_path``/``runs_dir`` 注入点供测试离线替换（生产默认
    DB 取数与固定 state 路径）。数据不足（<60 根）的标的记为
    data-insufficient，不中断其余标的。
    """
    targets = list(targets or DEFAULT_TARGETS)
    fetch = fetch or fetch_bars
    config = config or {}
    state_path = state_path or heartbeat_dirs()[0]
    runs_dir = runs_dir or heartbeat_dirs()[1]

    state = load_state(state_path, config)
    run_payload: dict = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "unified-engine",
        "results": [],
    }

    for symbol, name in targets:
        bars = fetch(symbol, days)
        if not bars or len(bars) < 60:
            run_payload["results"].append(
                {
                    "symbol": symbol,
                    "name": name,
                    "decision": "data-insufficient",
                    "error": f"数据不足：{len(bars) if bars else 0} 条",
                }
            )
            continue

        symbol_state = state["symbols"].setdefault(
            symbol, {"active_params": None, "last_best_result": None, "history": []})
        if symbol_state["active_params"] is None:
            symbol_state["active_params"] = _default_params(config)
        baseline_params = Params.from_dict(symbol_state["active_params"])
        baseline_result = run_symbol_backtest(symbol, bars, baseline_params)

        candidate_params, candidate_result = optimize_for_symbol(symbol, bars)

        if better(candidate_result, baseline_result):
            decision = "promote-candidate"
            active_params, active_result = candidate_params, candidate_result
            symbol_state["active_params"] = candidate_params.to_dict()
            symbol_state["last_best_result"] = candidate_result
        else:
            decision = "keep-baseline"
            active_params, active_result = baseline_params, baseline_result

        symbol_state["history"].append(
            {
                "run_at": run_payload["run_at"],
                "baseline_params": baseline_params.to_dict(),
                "baseline_result": baseline_result,
                "candidate_params": candidate_params.to_dict(),
                "candidate_result": candidate_result,
                "decision": decision,
                "active_params": active_params.to_dict(),
                "active_result": active_result,
            }
        )
        symbol_state["history"] = symbol_state["history"][-20:]

        run_payload["results"].append(
            {
                "symbol": symbol,
                "name": name,
                "baseline_params": baseline_params.to_dict(),
                "baseline_params_label": baseline_params.label(),
                "baseline_result": baseline_result,
                "candidate_params": candidate_params.to_dict(),
                "candidate_params_label": candidate_params.label(),
                "candidate_result": candidate_result,
                "decision": decision,
                "active_params": active_params.to_dict(),
                "active_params_label": active_params.label(),
                "active_result": active_result,
            }
        )

    save_state(state_path, state)
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (runs_dir / f"heartbeat_run_{stamp}.json").write_text(
        json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_payload


def update_summary_md(summary_path: Path, run_payload: dict) -> None:
    lines = [
        "# TradePilot 心跳参数巡检摘要（统一引擎口径）",
        "",
        f"- 最后执行：{run_payload['run_at']}",
        "- 口径：next_open 撮合 / T+1 / 涨跌停 / 佣金+印花税+滑点（tradepilot heartbeat）",
        "",
    ]
    for item in run_payload["results"]:
        lines.append(f"## {item['name']} ({item['symbol']})")
        lines.append("")
        if item.get("decision") == "data-insufficient":
            lines.extend([
                f"- 决策：**{item['decision']}**",
                f"- 错误：{item.get('error', '未知错误')}",
                "",
            ])
            continue
        lines.extend([
            f"- Baseline 参数：`{item['baseline_params_label']}`",
            f"- Baseline 收益：**{item['baseline_result']['total_return']:.2f}%**",
            f"- 候选参数：`{item['candidate_params_label']}`",
            f"- 候选收益：**{item['candidate_result']['total_return']:.2f}%**",
            f"- 决策：**{item['decision']}**",
            f"- 当前生效参数：`{item['active_params_label']}`",
            f"- 当前生效收益：**{item['active_result']['total_return']:.2f}%**",
            f"- 当前最大回撤：{item['active_result']['max_drawdown']:.2f}%",
            f"- 当前胜率：{item['active_result']['win_rate']:.2f}%",
            f"- 当前交易次数：{item['active_result']['total_trades']}",
            "",
        ])
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def maybe_notify(config: dict, run_payload: dict) -> None:
    feishu_cfg = config.get("notifiers", {}).get("feishu", {}) if config else {}
    if not feishu_cfg.get("enabled"):
        return

    from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier

    notifier = FeishuWebhookNotifier(
        feishu_cfg.get("webhook", ""),
        secret=feishu_cfg.get("secret"),
        dashboard_url=feishu_cfg.get("dashboard_url") or None,
    )
    lines = ["📈 TradePilot 心跳参数巡检摘要", f"时间：{run_payload['run_at']}", ""]
    for item in run_payload["results"]:
        if item.get("decision") == "data-insufficient":
            lines.append(f"• {item['name']}({item['symbol']}): 数据不足，未完成巡检")
            continue
        lines.extend([
            f"• {item['name']}({item['symbol']})",
            f"  - 决策：{item['decision']}",
            f"  - 当前收益：{item['active_result']['total_return']:.2f}%",
            f"  - 最大回撤：{item['active_result']['max_drawdown']:.2f}%",
            f"  - 胜率：{item['active_result']['win_rate']:.2f}%",
            f"  - 交易次数：{item['active_result']['total_trades']}",
            f"  - 参数：{item['active_params_label']}",
            "",
        ])
    notifier.send_text("\n".join(lines).rstrip())
