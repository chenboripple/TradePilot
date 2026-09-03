#!/usr/bin/env python3
"""
TradePilot 心跳优化脚本

闭环：
1. 读取当前生效参数（state / config）作为 baseline
2. 对目标股票执行回测
3. 网格搜索更优参数
4. 用候选参数重新回测
5. 若优于 baseline，则写入 state；否则回退 baseline
6. 记录 JSON + Markdown 报告
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "data" / "backtest" / "heartbeat_strategy_state.json"
RUNS_DIR = ROOT / "data" / "backtest" / "heartbeat_runs"
SUMMARY_MD = ROOT / "OPTIMIZATION_SUMMARY.md"
DATA_DIR = ROOT / "data"

TARGETS = [
    ("002022.SZ", "科华生物"),
    ("600309.SH", "万华化学"),
]


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


DEFAULT_GRID = {
    "ma": [(3, 10), (3, 12), (5, 15), (5, 20), (8, 21), (10, 30)],
    "rsi": [(10, 35, 65), (12, 30, 70), (14, 30, 70), (14, 35, 65)],
    "bb": [(14, 1.8), (20, 1.5), (20, 2.0), (26, 2.0)],
}


class Backtester:
    def __init__(self, params: Params):
        self.params = params
        self.ma_strategy = MovingAverageCross(fast=params.ma_fast, slow=params.ma_slow)
        self.rsi_strategy = RSI(
            period=params.rsi_period,
            oversold=params.rsi_oversold,
            overbought=params.rsi_overbought,
        )
        self.bb_strategy = BollingerBands(period=params.bb_period, std_dev=params.bb_std)

    def generate_signal(self, bar: Bar, history: List[Bar]) -> str:
        if len(history) < max(self.params.ma_slow, self.params.bb_period, 30):
            return "HOLD"

        self.ma_strategy.reset()
        self.rsi_strategy.reset()
        self.bb_strategy.reset()

        for prev_bar in history[:-1]:
            self.ma_strategy.on_bar(prev_bar)
            self.rsi_strategy.on_bar(prev_bar)
            self.bb_strategy.on_bar(prev_bar)

        ma_signal = self.ma_strategy.on_bar(bar)
        rsi_signal = self.rsi_strategy.on_bar(bar)
        bb_signal = self.bb_strategy.on_bar(bar)

        buy_score = 0
        sell_score = 0
        for sig in [ma_signal, rsi_signal, bb_signal]:
            if sig.side == Side.BUY:
                buy_score += 1
            elif sig.side == Side.SELL:
                sell_score += 1

        if buy_score >= 1 and sell_score == 0:
            return "BUY"
        if sell_score >= 1 and buy_score == 0:
            return "SELL"
        return "HOLD"

    def run(self, bars: List[Bar]) -> dict:
        initial_capital = 100000.0
        capital = initial_capital
        position = 0
        entry_price = 0.0
        trades = []
        equity_curve = []

        for i, bar in enumerate(bars):
            history = bars[: i + 1]
            signal = self.generate_signal(bar, history)

            if signal == "BUY" and position == 0:
                shares = int(capital * 0.95 / bar.close / 100) * 100
                if shares > 0:
                    cost = shares * bar.close * 1.0003
                    capital -= cost
                    position = shares
                    entry_price = bar.close

            elif signal == "SELL" and position > 0:
                revenue = position * bar.close * 0.9997
                pnl = (bar.close - entry_price) * position
                capital += revenue
                trades.append(
                    {
                        "date": bar.timestamp.strftime("%Y-%m-%d"),
                        "pnl": pnl,
                        "price": bar.close,
                        "shares": position,
                    }
                )
                position = 0
                entry_price = 0.0

            equity = capital + (position * bar.close if position > 0 else 0)
            equity_curve.append(equity)

        final_value = capital + (position * bars[-1].close if position > 0 else 0)
        total_return = (final_value - initial_capital) / initial_capital * 100
        win_rate = sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100 if trades else 0.0

        peak = initial_capital
        max_drawdown = 0.0
        for equity in equity_curve:
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100 if peak else 0.0
            max_drawdown = max(max_drawdown, dd)

        score = total_return - max_drawdown * 0.35 + win_rate * 0.02

        return {
            "initial_capital": initial_capital,
            "final_capital": final_value,
            "total_return": total_return,
            "total_trades": len(trades),
            "win_rate": win_rate,
            "max_drawdown": max_drawdown,
            "score": score,
            "trades": trades,
        }


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(config: dict) -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))

    default = config["strategies"]
    return {
        "updated_at": None,
        "symbols": {
            code: {
                "active_params": {
                    "ma_fast": default["ma_cross"]["params"]["fast"],
                    "ma_slow": default["ma_cross"]["params"]["slow"],
                    "rsi_period": default["rsi"]["params"]["period"],
                    "rsi_oversold": default["rsi"]["params"]["oversold"],
                    "rsi_overbought": default["rsi"]["params"]["overbought"],
                    "bb_period": default["bollinger"]["params"]["period"],
                    "bb_std": default["bollinger"]["params"]["std_dev"],
                },
                "last_best_result": None,
                "history": [],
            }
            for code, _ in TARGETS
        },
    }


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _csv_path_for(ts_code: str) -> Path:
    return DATA_DIR / f"{ts_code}.csv"


def _bars_to_dataframe(bars: List[Bar]) -> pd.DataFrame:
    rows = [
        {
            "trade_date": bar.timestamp.strftime("%Y%m%d"),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "vol": bar.volume / 100.0,
        }
        for bar in bars
    ]
    return pd.DataFrame(rows)


def _dataframe_to_bars(df: pd.DataFrame) -> List[Bar]:
    bars: List[Bar] = []
    for _, row in df.iterrows():
        try:
            trade_date = datetime.strptime(str(row["trade_date"]), "%Y%m%d")
            bars.append(
                Bar(
                    timestamp=trade_date,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("vol", 0)) * 100.0,
                )
            )
        except Exception:
            continue
    return bars


def fetch_bars(loader: TushareDataLoader, ts_code: str, days: int = 365, refresh_full: bool = False) -> List[Bar]:
    csv_path = _csv_path_for(ts_code)
    end_date = datetime.now()
    window_start = end_date - timedelta(days=days)
    
    # 缓存过期阈值：如果缓存最新日期距离今天超过7天，视为过期，需要全量刷新
    CACHE_STALE_DAYS = 7

    cached_df = pd.DataFrame()
    cache_is_stale = False
    
    if csv_path.exists() and not refresh_full:
        try:
            cached_df = pd.read_csv(csv_path, dtype={"trade_date": str})
            if not cached_df.empty:
                cached_df = cached_df[[c for c in ["trade_date", "open", "high", "low", "close", "vol"] if c in cached_df.columns]]
                cached_df = cached_df.dropna(subset=["trade_date", "open", "high", "low", "close"])
                cached_df["trade_date"] = cached_df["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
                
                # 检查缓存是否过期
                cached_df["trade_date_dt"] = pd.to_datetime(cached_df["trade_date"], format="%Y%m%d", errors="coerce")
                cached_df = cached_df.dropna(subset=["trade_date_dt"])
                if not cached_df.empty:
                    latest_cached = cached_df["trade_date_dt"].max().to_pydatetime()
                    days_since_last_update = (end_date - latest_cached).days
                    if days_since_last_update > CACHE_STALE_DAYS:
                        print(f"[缓存过期] {ts_code} 缓存最新日期: {latest_cached.strftime('%Y-%m-%d')}, 距今 {days_since_last_update} 天，将全量刷新")
                        cache_is_stale = True
                        cached_df = pd.DataFrame()  # 清空缓存，强制全量刷新
        except Exception as e:
            print(f"[缓存读取失败] {ts_code}: {e}")
            cached_df = pd.DataFrame()

    newer_df = pd.DataFrame()
    older_df = pd.DataFrame()
    if not cached_df.empty and not cache_is_stale:
        cached_df["trade_date_dt"] = pd.to_datetime(cached_df["trade_date"], format="%Y%m%d", errors="coerce")
        cached_df = cached_df.dropna(subset=["trade_date_dt"]).sort_values("trade_date_dt")
        latest_cached = cached_df["trade_date_dt"].max().to_pydatetime()
        earliest_cached = cached_df["trade_date_dt"].min().to_pydatetime()

        newer_start = max(window_start, latest_cached + timedelta(days=1))
        if newer_start <= end_date:
            newer_bars = list(
                loader.load_bars(
                    ts_code,
                    newer_start.strftime("%Y%m%d"),
                    end_date.strftime("%Y%m%d"),
                )
            )
            if newer_bars:
                newer_df = _bars_to_dataframe(newer_bars)

        if earliest_cached > window_start:
            older_end = earliest_cached - timedelta(days=1)
            if window_start <= older_end:
                older_bars = list(
                    loader.load_bars(
                        ts_code,
                        window_start.strftime("%Y%m%d"),
                        older_end.strftime("%Y%m%d"),
                    )
                )
                if older_bars:
                    older_df = _bars_to_dataframe(older_bars)
    else:
        all_bars = list(
            loader.load_bars(
                ts_code,
                window_start.strftime("%Y%m%d"),
                end_date.strftime("%Y%m%d"),
            )
        )
        if all_bars:
            newer_df = _bars_to_dataframe(all_bars)

    if cached_df.empty and newer_df.empty and older_df.empty:
        return []

    frames = []
    if not cached_df.empty:
        frames.append(cached_df[["trade_date", "open", "high", "low", "close", "vol"]])
    if not older_df.empty:
        frames.append(older_df[["trade_date", "open", "high", "low", "close", "vol"]])
    if not newer_df.empty:
        frames.append(newer_df[["trade_date", "open", "high", "low", "close", "vol"]])

    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if merged.empty:
        return []

    merged["trade_date"] = merged["trade_date"].astype(str)
    merged = merged.drop_duplicates(subset=["trade_date"], keep="last")
    merged["trade_date_dt"] = pd.to_datetime(merged["trade_date"], format="%Y%m%d", errors="coerce")
    merged = merged.dropna(subset=["trade_date_dt"])
    merged = merged[merged["trade_date_dt"] >= pd.Timestamp(window_start.date())]
    merged = merged.sort_values("trade_date_dt")

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    merged[["trade_date", "open", "high", "low", "close", "vol"]].to_csv(csv_path, index=False)

    return _dataframe_to_bars(merged[["trade_date", "open", "high", "low", "close", "vol"]])


def optimize_for_symbol(bars: List[Bar]) -> Tuple[Params, dict]:
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
                result = Backtester(params).run(bars)
                results.append((params, result))

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


def write_run_report(run_payload: dict) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RUNS_DIR / f"heartbeat_run_{ts}.json"
    path.write_text(json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_summary_md(run_payload: dict):
    lines = [
        "# TradePilot 策略优化摘要",
        "",
        f"- 最后执行：{run_payload['run_at']}",
        f"- 模式：heartbeat_tradepilot.py 自动闭环 ({run_payload.get('mode', 'unknown')})",
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

    SUMMARY_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def maybe_notify(config: dict, run_payload: dict):
    feishu_cfg = config.get("notifiers", {}).get("feishu", {})
    if not feishu_cfg.get("enabled"):
        return

    notifier = FeishuWebhookNotifier(feishu_cfg.get("webhook", ""), feishu_cfg.get("secret"))
    lines = ["📈 TradePilot 心跳优化摘要", f"时间：{run_payload['run_at']}", ""]

    for item in run_payload["results"]:
        if item.get("decision") == "data-insufficient":
            lines.append(f"• {item['name']}({item['symbol']}): 数据不足，未完成优化")
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

    notifier._send_text({"msg_type": "text", "content": {"text": "\n".join(lines).rstrip()}})


def main():
    # 过拟合防护：网格再拟合默认关闭。
    # 在短窗口上反复挑选最优参数只会拟合噪声（详见项目分析），
    # 确需运行时必须显式传 --allow-refit 或设置 TRADEPILOT_AUTOFIT=1。
    allow_refit = "--allow-refit" in sys.argv or os.getenv("TRADEPILOT_AUTOFIT") == "1"
    if not allow_refit:
        print(
            "⚠️ 自动参数再拟合已默认停用（过拟合风险）。\n"
            "   本脚本不再搜索/写回新参数。如确有需要：\n"
            "   python heartbeat_tradepilot.py --allow-refit\n"
            "   或设置环境变量 TRADEPILOT_AUTOFIT=1"
        )
        return

    refresh_full = "--refresh-full" in sys.argv

    config = load_config()
    state = load_state(config)
    loader = TushareDataLoader(config["tushare"]["token"], rate_limit_delay=config["tushare"].get("rate_limit_delay", 1.5))

    run_payload = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "refresh-full" if refresh_full else "incremental-cache",
        "results": [],
    }

    for symbol, name in TARGETS:
        bars = fetch_bars(loader, symbol, 365, refresh_full=refresh_full)
        if len(bars) < 60:
            run_payload["results"].append(
                {
                    "symbol": symbol,
                    "name": name,
                    "decision": "data-insufficient",
                    "error": f"数据不足：{len(bars)} 条",
                }
            )
            continue

        symbol_state = state["symbols"].setdefault(symbol, {"active_params": None, "last_best_result": None, "history": []})
        baseline_params = Params.from_dict(symbol_state["active_params"])
        baseline_result = Backtester(baseline_params).run(bars)

        candidate_params, candidate_result = optimize_for_symbol(bars)

        if better(candidate_result, baseline_result):
            decision = "promote-candidate"
            active_params = candidate_params
            active_result = candidate_result
            symbol_state["active_params"] = candidate_params.to_dict()
            symbol_state["last_best_result"] = candidate_result
        else:
            decision = "keep-baseline"
            active_params = baseline_params
            active_result = baseline_result

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

    save_state(state)
    report_path = write_run_report(run_payload)
    update_summary_md(run_payload)
    maybe_notify(config, run_payload)

    print("=" * 80)
    print("✅ TradePilot 心跳优化完成")
    print(f"状态文件：{STATE_PATH}")
    print(f"运行记录：{report_path}")
    print(f"摘要文件：{SUMMARY_MD}")
    print("=" * 80)


if __name__ == "__main__":
    main()
