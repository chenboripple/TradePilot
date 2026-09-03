#!/usr/bin/env python3
# 一次性研究脚本，归档于 experiments/，不在产品路径维护
"""对历史寻优过的三只票，按寻优结论的策略跑 1年预热 + 3个月评估 回测"""
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent / "src"))
import pandas as pd

from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.combo_vote import ComboVoteStrategy
from ripple_tradePilot.strategies.dual_thrust import DualThrust
from ripple_tradePilot.risk.manager import RiskConfig
from ripple_tradePilot.backtest.engine import run_backtest

ROOT = Path(__file__).parent
EVAL_DAYS = 91

SPECS = [
    {
        'code': '000868.SZ', 'name': '安凯客车',
        'file': '/tmp/mx_out/mx_data_安凯客车近16个月每个交易日的开盘价_最高价_最低价_收盘价_成交量.xlsx',
        'strategy': 'MA(10,30)',
        'build': lambda: MovingAverageCross(fast=10, slow=30),
    },
    {
        'code': '000999.SZ', 'name': '华润三九',
        'file': '/tmp/mx_out/mx_data_华润三九近16个月每个交易日的开盘价_最高价_最低价_收盘价_成交量.xlsx',
        'strategy': 'combo MA(5,15)+RSI(16,35/65) th=1',
        'build': lambda: ComboVoteStrategy(
            [('ma', MovingAverageCross(fast=5, slow=15)),
             ('rsi', RSI(period=16, oversold=35, overbought=65))],
            vote_threshold=1),
    },
    {
        'code': '603039.SH', 'name': '泛海微',
        'file': '/tmp/mx_out/mx_data_泛海微近16个月每个交易日的开盘价_最高价_最低价_收盘价_成交量.xlsx',
        'strategy': 'DualThrust(lookback=3,k1=0.5,k2=0.5)',
        'build': lambda: DualThrust(lookback=3, k1=0.5, k2=0.5),
    },
]


def load_bars(path):
    df = pd.read_excel(path)
    date_col = next(c for c in df.columns if 'date' in c.lower() or '日期' in c)
    def num(key):
        c = next(c for c in df.columns if key in c)
        return pd.to_numeric(df[c].astype(str).str.replace('[^0-9.\\-]', '', regex=True), errors='coerce')
    dates = pd.to_datetime(df[date_col].astype(str).str[:10], errors='coerce')
    o, h, l, cl, v = num('开盘'), num('最高'), num('最低'), num('收盘'), num('成交')
    bars = []
    for i in range(len(df)):
        if pd.isna(dates.iloc[i]) or pd.isna(cl.iloc[i]):
            continue
        bars.append(Bar(timestamp=dates.iloc[i].to_pydatetime(),
                        open=float(o.iloc[i]), high=float(h.iloc[i]),
                        low=float(l.iloc[i]), close=float(cl.iloc[i]),
                        volume=float(v.iloc[i]) if not pd.isna(v.iloc[i]) else 0.0))
    bars.sort(key=lambda b: b.timestamp)
    return bars


def metrics(res, initial=100000.0):
    final = res.equity_curve[-1]
    peak, mdd = initial, 0.0
    for eq in res.equity_curve:
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    trades, op = [], None
    for f in res.fills:
        if f.side == Side.BUY:
            op = f
        elif f.side == Side.SELL and op is not None:
            trades.append(round((f.price / op.price - 1) * 100, 2))
            op = None
    wins = sum(1 for r in trades if r > 0)
    holding = bool(res.fills) and res.fills[-1].side == Side.BUY
    return {
        'final': round(final, 2),
        'ret_pct': round((final / initial - 1) * 100, 2),
        'mdd_pct': round(mdd * 100, 2),
        'fills': len(res.fills), 'closed': len(trades),
        'win_rate_pct': round(wins / len(trades) * 100, 1) if trades else None,
        'trade_rets_pct': trades, 'holding': holding,
        'fill_detail': [{'date': str(f.timestamp.date()), 'side': f.side.value, 'price': round(f.price, 3)}
                        for f in res.fills],
    }


def main():
    today = datetime.now()
    eval_start = (today - timedelta(days=EVAL_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    results = {}
    for spec in SPECS:
        bars = load_bars(spec['file'])
        warm = [b for b in bars if b.timestamp < eval_start]
        evalb = [b for b in bars if b.timestamp >= eval_start]
        bh = evalb[-1].close / evalb[0].open - 1
        print(f"\n{'='*16} {spec['code']} {spec['name']} [{spec['strategy']}] {'='*16}")
        print(f"数据 {bars[0].timestamp.date()}~{bars[-1].timestamp.date()} 共{len(bars)}根 | 预热{len(warm)}根 | 评估{len(evalb)}根 ({evalb[0].timestamp.date()}~{evalb[-1].timestamp.date()})")
        print(f"买入持有基准: {bh*100:+.2f}%")

        strat = spec['build']()
        strat.reset()
        for b in warm:
            strat.on_bar(b)
        res = run_backtest(strat, evalb, initial_cash=100000.0, fee_rate=0.0005,
                           risk_config=RiskConfig())
        m = metrics(res)
        wr = f"{m['win_rate_pct']}%" if m['win_rate_pct'] is not None else 'N/A(无平仓)'
        print(f"期末 {m['final']:,.0f} ({m['ret_pct']:+.2f}%) | 最大回撤 {m['mdd_pct']:.2f}% | "
              f"成交{m['fills']}笔(平仓{m['closed']}) 胜率 {wr} | 期末{'持仓' if m['holding'] else '空仓'}")
        print(f"单笔收益%: {m['trade_rets_pct']}")
        for f in m['fill_detail']:
            print(f"  {f['date']} {f['side']} @ {f['price']}")
        results[f"{spec['code']} {spec['name']}"] = {
            'strategy': spec['strategy'],
            'buy_hold_pct': round(bh * 100, 2),
            'eval_start': str(evalb[0].timestamp.date()), 'eval_end': str(evalb[-1].timestamp.date()),
            'warmup_bars': len(warm), 'eval_bars': len(evalb), **m,
        }

    out = ROOT / 'output' / f'backtest_3m_warmup1y_mx_more_{today.strftime("%Y%m%d")}.json'
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n结果已保存: {out}")


if __name__ == '__main__':
    main()
