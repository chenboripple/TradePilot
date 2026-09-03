#!/usr/bin/env python3
# 一次性研究脚本，归档于 experiments/，不在产品路径维护
"""用东财(mx-data)数据跑: 1年预热 + 3个月评估 回测"""
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent / "src"))
import yaml
import pandas as pd

from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.strategies.combo_vote import ComboVoteStrategy
from ripple_tradePilot.risk.manager import RiskConfig
from ripple_tradePilot.backtest.engine import run_backtest

ROOT = Path(__file__).parent
EVAL_DAYS = 91

FILES = {
    '002022.SZ': '/tmp/mx_out/mx_data_科华生物近16个月每个交易日的开盘价_最高价_最低价_收盘价_成交量.xlsx',
    '600309.SH': '/tmp/mx_out/mx_data_万华化学近16个月每个交易日的开盘价_最高价_最低价_收盘价_成交量.xlsx',
}


def load_bars(path):
    df = pd.read_excel(path)
    # 列名类似 date/日期, 开盘价, 最高价, 最低价, 收盘价, 成交量
    cols = {c: c for c in df.columns}
    date_col = next(c for c in df.columns if 'date' in c.lower() or '日期' in c)
    def num(col_key):
        c = next(c for c in df.columns if col_key in c)
        return pd.to_numeric(df[c].astype(str).str.replace('[^0-9.\\-]', '', regex=True), errors='coerce')
    bars = []
    dates = pd.to_datetime(df[date_col].astype(str).str[:10], errors='coerce')
    o, h, l, cl, v = num('开盘'), num('最高'), num('最低'), num('收盘'), num('成交')
    for i in range(len(df)):
        if pd.isna(dates.iloc[i]) or pd.isna(cl.iloc[i]):
            continue
        bars.append(Bar(timestamp=dates.iloc[i].to_pydatetime(),
                        open=float(o.iloc[i]), high=float(h.iloc[i]),
                        low=float(l.iloc[i]), close=float(cl.iloc[i]),
                        volume=float(v.iloc[i]) if not pd.isna(v.iloc[i]) else 0.0))
    bars.sort(key=lambda b: b.timestamp)
    return bars


def build(profile, threshold):
    ma = MovingAverageCross(fast=profile.get('ma_fast', 5), slow=profile.get('ma_slow', 20))
    rsi = RSI(period=profile.get('rsi_period', 14), oversold=profile.get('rsi_oversold', 30),
              overbought=profile.get('rsi_overbought', 70))
    bb = BollingerBands(period=profile.get('bb_period', 20), std_dev=profile.get('bb_std', 2.0))
    return ComboVoteStrategy([('ma', ma), ('rsi', rsi), ('bb', bb)], vote_threshold=threshold)


def run(strategy, eval_bars, initial=100000.0):
    return run_backtest(strategy, eval_bars, initial_cash=initial, fee_rate=0.0005,
                        risk_config=RiskConfig())


def report(name, res, initial=100000.0):
    final = res.equity_curve[-1]
    peak, mdd = initial, 0.0
    for eq in res.equity_curve:
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    n_sells = len([f for f in res.fills if f.side == Side.SELL])
    holding = bool(res.fills) and res.fills[-1].side == Side.BUY
    # 配对胜率
    trades, op = [], None
    for f in res.fills:
        if f.side == Side.BUY:
            op = f
        elif f.side == Side.SELL and op is not None:
            trades.append(f.price / op.price - 1)
            op = None
    wins = sum(1 for r in trades if r > 0)
    return {'final': final, 'ret_pct': (final / initial - 1) * 100, 'mdd_pct': mdd * 100,
            'fills': len(res.fills), 'closed': len(trades), 'holding': holding,
            'win_rate': (wins / len(trades) * 100) if trades else None,
            'trade_rets': [round(r * 100, 2) for r in trades], 'fills_detail': res.fills}


def main():
    config = yaml.safe_load(open(ROOT / 'config.yaml', encoding='utf-8'))
    profiles = config.get('strategy_profiles', {})
    today = datetime.now()
    eval_start = (today - timedelta(days=EVAL_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)

    results = {}
    for sym in config.get('symbols', []):
        code, name, pname = sym['code'], sym['name'], sym['strategy_profile']
        profile = profiles[pname]
        bars = load_bars(FILES[code])
        warm = [b for b in bars if b.timestamp < eval_start]
        evalb = [b for b in bars if b.timestamp >= eval_start]
        bh = evalb[-1].close / evalb[0].open - 1

        print(f"\n{'='*18} {code} {name} [{pname}] {'='*18}")
        print(f"数据 {bars[0].timestamp.date()}~{bars[-1].timestamp.date()} 共{len(bars)}根 | 预热{len(warm)}根 | 评估{len(evalb)}根 ({evalb[0].timestamp.date()}~{evalb[-1].timestamp.date()})")
        print(f"买入持有基准: {bh*100:+.2f}%")

        entry = {'buy_hold_pct': round(bh * 100, 2), 'eval_start': str(evalb[0].timestamp.date()),
                 'eval_end': str(evalb[-1].timestamp.date()), 'warmup_bars': len(warm), 'eval_bars': len(evalb)}

        # 信号诊断（threshold=2，与监控一致）
        combo = build(profile, 2)
        for b in warm:
            combo.on_bar(b)
        siglog = []
        for b in evalb:
            sigs = {n: s.on_bar(b) for n, s in combo.strategies}
            buys = [n for n, s in sigs.items() if s.side == Side.BUY]
            sells = [n for n, s in sigs.items() if s.side == Side.SELL]
            if buys or sells:
                siglog.append((str(b.timestamp.date()), buys, sells))
        print("评估窗口信号明细 (子策略):")
        for d, buys, sells in siglog:
            tag = '冲突' if buys and sells else ('买' if buys else '卖')
            print(f"  {d} [{tag}] buy={buys or '-'} sell={sells or '-'}")
        entry['signals'] = [{'date': d, 'buy': buys, 'sell': sells} for d, buys, sells in siglog]

        for th in (2, 1):
            strat = build(profile, th)
            for b in warm:
                strat.on_bar(b)
            res = run(strat, evalb)
            m = report(f"{code}", res)
            detail = {k: v for k, v in m.items() if k != 'fills_detail'}
            wr = f"{m['win_rate']:.0f}%" if m['win_rate'] is not None else 'N/A(无平仓)'
            print(f"vote_threshold={th}: 期末 {m['final']:,.0f} ({m['ret_pct']:+.2f}%) | 最大回撤 {m['mdd_pct']:.2f}% | "
                  f"成交{m['fills']}笔(平仓{m['closed']}) 胜率 {wr} | 期末{'持仓' if m['holding'] else '空仓'} | 单笔收益% {m['trade_rets']}")
            entry[f'threshold_{th}'] = detail
            if th == 2:
                entry['fills'] = [{'date': str(f.timestamp.date()), 'side': f.side.value, 'price': round(f.price, 3)}
                                  for f in m['fills_detail']]

        results[f"{code} {name}"] = entry

    out = ROOT / 'output' / f'backtest_3m_warmup1y_mx_{today.strftime("%Y%m%d")}.json'
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n结果已保存: {out}")


if __name__ == '__main__':
    main()
