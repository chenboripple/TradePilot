#!/usr/bin/env python3
# 一次性研究脚本，归档于 experiments/，不在产品路径维护
"""
对 config.yaml 中当前股票策略做回测：
- 数据范围：约 1 年预热 + 最近 3 个月评估
- 预热期只喂策略、不计绩效；绩效只统计评估窗口（最近 3 个月）
- 使用项目自带 run_backtest 引擎（含 8% 止损 / 20% 止盈 / 20% 最大回撤熔断）
- combo_vote 投票阈值与监控路径一致，默认 2 票
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent / "src"))

import yaml

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.models.types import Bar
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.strategies.combo_vote import ComboVoteStrategy
from ripple_tradePilot.risk.manager import RiskConfig
from ripple_tradePilot.backtest.engine import run_backtest

ROOT = Path(__file__).parent
EVAL_DAYS = 91  # ~3 个月


def df_to_bars(df) -> list[Bar]:
    bars = []
    for _, row in df.iterrows():
        ts = datetime.strptime(str(row['trade_date']), '%Y%m%d')
        bars.append(Bar(
            timestamp=ts,
            open=float(row['open']),
            high=float(row['high']),
            low=float(row['low']),
            close=float(row['close']),
            volume=float(row.get('vol', 0) or 0),
        ))
    return bars


def build_strategy(profile: dict) -> ComboVoteStrategy:
    kind = profile.get('kind', 'combo_vote')
    if kind != 'combo_vote':
        raise ValueError(f"暂不支持的 profile kind: {kind}")
    ma = MovingAverageCross(fast=profile.get('ma_fast', 5), slow=profile.get('ma_slow', 20))
    rsi = RSI(
        period=profile.get('rsi_period', 14),
        oversold=profile.get('rsi_oversold', 30),
        overbought=profile.get('rsi_overbought', 70),
    )
    bb = BollingerBands(period=profile.get('bb_period', 20), std_dev=profile.get('bb_std', 2.0))
    return ComboVoteStrategy(
        [('ma_cross', ma), ('rsi', rsi), ('bollinger', bb)],
        vote_threshold=profile.get('vote_threshold', 2),
    )


def metrics(equity_curve, fills, initial_cash, eval_start_ts):
    if not equity_curve:
        return None
    final = equity_curve[-1]
    total_ret = final / initial_cash - 1
    peak, max_dd = equity_curve[0], 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        max_dd = max(max_dd, dd)

    # 把 fills 按 buy/sell 配对计算胜率（评估窗口内的交易）
    trades, open_trade = [], None
    for f in fills:
        if f.timestamp < eval_start_ts:
            continue
        if f.side.name == 'BUY':
            open_trade = {'buy': f.price, 'qty': f.quantity, 'fee': f.fee, 'date': f.timestamp}
        elif f.side.name == 'SELL' and open_trade is not None:
            pnl = (f.price - open_trade['buy']) * f.quantity - open_trade['fee'] - f.fee
            trades.append({'entry': open_trade['date'].strftime('%Y-%m-%d'),
                           'exit': f.timestamp.strftime('%Y-%m-%d'),
                           'pnl': pnl,
                           'ret': f.price / open_trade['buy'] - 1})
            open_trade = None
    wins = [t for t in trades if t['pnl'] > 0]
    win_rate = len(wins) / len(trades) if trades else None

    # 简易年化（按评估窗口交易日）
    n_days = len(equity_curve)
    ann_ret = (final / initial_cash) ** (252 / max(n_days, 1)) - 1 if final > 0 else None

    # 简易 sharpe：日收益
    rets = [equity_curve[i] / equity_curve[i - 1] - 1 for i in range(1, len(equity_curve))]
    if len(rets) > 2 and sum((r - sum(rets) / len(rets)) ** 2 for r in rets) > 0:
        mean = sum(rets) / len(rets)
        std = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        sharpe = mean / std * (252 ** 0.5) if std > 0 else None
    else:
        sharpe = None

    return {
        'final_equity': round(final, 2),
        'total_return_pct': round(total_ret * 100, 2),
        'annual_return_pct': round(ann_ret * 100, 2) if ann_ret is not None else None,
        'max_drawdown_pct': round(max_dd * 100, 2),
        'sharpe': round(sharpe, 2) if sharpe is not None else None,
        'closed_trades': len(trades),
        'win_rate_pct': round(win_rate * 100, 1) if win_rate is not None else None,
        'trades': trades,
    }


def main():
    with open(ROOT / 'config.yaml', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    token = config['tushare']['token']
    rate_delay = config['tushare'].get('rate_limit_delay', 1.5)
    loader = TushareDataLoader(token, rate_limit_delay=rate_delay)
    profiles = config.get('strategy_profiles', {})

    today = datetime.now()
    eval_start = today - timedelta(days=EVAL_DAYS)
    fetch_start = today - timedelta(days=EVAL_DAYS + 370)  # 1 年预热 + 余量

    results = {}
    for sym in config.get('symbols', []):
        code, name, prof_name = sym['code'], sym['name'], sym['strategy_profile']
        profile = profiles.get(prof_name)
        if not profile:
            print(f"⚠️ {code} {name}: 找不到策略画像 {prof_name}")
            continue

        print(f"\n=== {code} {name} (策略: {prof_name}) ===")
        df = loader.get_daily_bars(code, fetch_start.strftime('%Y%m%d'), today.strftime('%Y%m%d'))
        if df is None or df.empty:
            print("⚠️ 无数据")
            continue

        bars = df_to_bars(df)
        eval_start_ts = eval_start.replace(hour=0, minute=0, second=0, microsecond=0)
        warmup_bars = [b for b in bars if b.timestamp < eval_start_ts]
        eval_bars = [b for b in bars if b.timestamp >= eval_start_ts]
        print(f"数据: {bars[0].timestamp.date()} ~ {bars[-1].timestamp.date()}, "
              f"预热 {len(warmup_bars)} 根, 评估 {len(eval_bars)} 根")

        strategy = build_strategy(profile)
        strategy.reset()
        # 预热：只喂 bar，不计绩效（on_bar 内部会累积指标状态）
        for b in warmup_bars:
            strategy.on_bar(b)

        initial_cash = 100000.0
        risk_cfg = RiskConfig(max_position_pct=1.0, stop_loss_pct=0.08,
                              take_profit_pct=0.20, max_drawdown_pct=0.20)
        result = run_backtest(strategy, eval_bars, initial_cash=initial_cash,
                              fee_rate=0.0005, risk_config=risk_cfg)
        m = metrics(result.equity_curve, result.fills, initial_cash, eval_start_ts)
        results[f"{code} {name}"] = m

        print(f"窗口: {eval_bars[0].timestamp.date()} ~ {eval_bars[-1].timestamp.date()}")
        print(f"期末资金: {m['final_equity']:,.0f} (初始 {initial_cash:,.0f})")
        print(f"总收益: {m['total_return_pct']:+.2f}%  年化: {m['annual_return_pct']}%")
        print(f"最大回撤: {m['max_drawdown_pct']:.2f}%  Sharpe: {m['sharpe']}")
        print(f"平仓交易: {m['closed_trades']} 笔  胜率: {m['win_rate_pct']}%")
        for t in m['trades']:
            print(f"  {t['entry']} → {t['exit']}  收益 {t['ret']*100:+.2f}%  盈亏 {t['pnl']:+,.0f}")
        # 持仓中提示
        last_fill_side = result.fills[-1].side.name if result.fills else None
        if last_fill_side == 'BUY':
            print("⚠️ 窗口结束时仍持仓（未平仓，收益按市值计）")

    out = ROOT / 'output' / f'backtest_3m_warmup1y_{today.strftime("%Y%m%d")}.json'
    out.parent.mkdir(exist_ok=True)
    import json
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n结果已保存: {out}")


if __name__ == '__main__':
    main()
