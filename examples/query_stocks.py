#!/usr/bin/env python3
"""
查询指定股票的日线数据并分析支撑位/压力位
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../src'))

from datetime import datetime, timedelta
import pandas as pd
from ripple_tradePilot.data.tushare_loader import TushareDataLoader

# Tushare Token 从示例中获取
TOKEN = "your_tushare_token_here"

# 需要查询的股票列表
STOCKS = [
    ("迈瑞医疗", "300760.SZ"),
    ("新产业", "300832.SZ"),
    ("中际旭创", "300308.SZ"),
    ("工业富联", "601138.SH"),
]

def calculate_support_resistance(df: pd.DataFrame, n_levels: int = 3) -> tuple:
    """
    根据近期高点低点计算支撑位和压力位
    简单算法：从最近N个高低点中提取关键价位
    """
    # 按时间排序
    df = df.sort_values('trade_date', ascending=True).reset_index(drop=True)
    
    # 获取最近6个月的数据点
    recent = df.tail(120)
    
    # 找局部高点（压力位）
    highs = []
    for i in range(2, len(recent) - 2):
        if recent.iloc[i].high > recent.iloc[i-1].high and recent.iloc[i].high > recent.iloc[i-2].high and \
           recent.iloc[i].high > recent.iloc[i+1].high and recent.iloc[i].high > recent.iloc[i+2].high:
            highs.append(recent.iloc[i].high)
    
    # 找局部低点（支撑位）
    lows = []
    for i in range(2, len(recent) - 2):
        if recent.iloc[i].low < recent.iloc[i-1].low and recent.iloc[i].low < recent.iloc[i-2].low and \
           recent.iloc[i].low < recent.iloc[i+1].low and recent.iloc[i].low < recent.iloc[i+2].low:
            lows.append(recent.iloc[i].low)
    
    # 排序并取最近的N个
    highs = sorted(highs, reverse=True)[:n_levels]
    lows = sorted(lows, reverse=False)[:n_levels]
    
    # 如果数量不够，用均线和百分比回撤补充
    if len(highs) < n_levels:
        max_price = recent['high'].max()
        min_price = recent['low'].min()
        if len(highs) == 0:
            highs = [max_price]
        # 添加斐波那契回撤位
        highs.append(max_price * 0.95)
        highs.append(max_price * 0.9)
        highs = sorted(highs, reverse=True)[:n_levels]
    
    if len(lows) < n_levels:
        max_price = recent['high'].max()
        min_price = recent['low'].min()
        if len(lows) == 0:
            lows = [min_price]
        lows.append(min_price * 1.05)
        lows.append(min_price * 1.1)
        lows = sorted(lows, reverse=False)[:n_levels]
    
    return sorted(lows), sorted(highs, reverse=True)

def analyze_trend(df: pd.DataFrame) -> dict:
    """分析近期走势"""
    df = df.sort_values('trade_date', ascending=True).reset_index(drop=True)
    recent_20 = df.tail(20)
    recent_60 = df.tail(60)
    
    # 计算均线
    ma20 = recent_20['close'].mean()
    ma60 = recent_60['close'].mean()
    
    # 当前价格
    current_close = df.iloc[-1]['close']
    
    # 涨跌幅
    start_6m = df.iloc[0]['close']
    change_6m = ((current_close - start_6m) / start_6m) * 100
    
    # 近一个月涨跌幅
    if len(df) >= 20:
        start_1m = df.iloc[-21]['close'] if len(df) > 20 else df.iloc[0]['close']
        change_1m = ((current_close - start_1m) / start_1m) * 100
    else:
        change_1m = change_6m
    
    # 判断趋势
    if current_close > ma20 > ma60:
        trend = "多头排列，上升趋势"
        trend_strength = 2
    elif current_close < ma20 < ma60:
        trend = "空头排列，下降趋势"
        trend_strength = -2
    elif ma20 > ma60 and current_close < ma20:
        trend = "上升趋势回调"
        trend_strength = 0
    elif ma20 < ma60 and current_close > ma20:
        trend = "下降趋势反弹"
        trend_strength = 0
    else:
        trend = "震荡整理"
        trend_strength = 0
    
    return {
        'current_close': current_close,
        'ma20': ma20,
        'ma60': ma60,
        'change_6m': change_6m,
        'change_1m': change_1m,
        'trend': trend,
        'trend_strength': trend_strength
    }

def main():
    loader = TushareDataLoader(TOKEN)
    
    # 计算6个月前的日期
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
    
    print("=" * 80)
    print(f"股票日线数据查询（近6个月）")
    print(f"查询时间: {datetime.now()}")
    print("=" * 80)
    print()
    
    results = []
    
    for name, ts_code in STOCKS:
        print(f"正在查询 {name} ({ts_code})...")
        
        # 获取日线数据
        df = loader.get_daily_bars(ts_code, start_date=start_date, end_date=end_date)
        
        if df is None or len(df) == 0:
            print(f"  ❌ 未获取到数据，跳过")
            continue
        
        # 获取实时行情
        quote = loader.get_realtime_quote(ts_code)
        
        # 分析走势
        analysis = analyze_trend(df)
        
        # 计算支撑位和压力位
        supports, resistances = calculate_support_resistance(df)
        
        # 保存结果
        result = {
            'name': name,
            'ts_code': ts_code,
            'bars': len(df),
            'df': df,
            'quote': quote,
            'analysis': analysis,
            'supports': supports,
            'resistances': resistances
        }
        results.append(result)
        
        # 缓存数据到CSV
        output_dir = Path("./output")
        output_dir.mkdir(exist_ok=True)
        df.to_csv(output_dir / f"{ts_code}.csv", index=False)
        
        print(f"  ✅ 获取 {len(df)} 条日线数据")
    
    print()
    print("=" * 80)
    print("分析结果")
    print("=" * 80)
    print()
    
    for result in results:
        print(f"## {result['name']} ({result['ts_code']})")
        print()
        
        current_price = result['quote']['price'] if result['quote'] else result['analysis']['current_close']
        print(f"- **当前价格**: {current_price:.2f} 元")
        print(f"- **近6个月涨跌幅**: {result['analysis']['change_6m']:.2f}%")
        print(f"- **近1个月涨跌幅**: {result['analysis']['change_1m']:.2f}%")
        print(f"- **走势**: {result['analysis']['trend']}")
        print(f"- MA20: {result['analysis']['ma20']:.2f}, MA60: {result['analysis']['ma60']:.2f}")
        print()
        
        print(f"- **关键支撑位**: {', '.join([f'{s:.2f}' for s in result['supports']])}")
        print(f"- **关键压力位**: {', '.join([f'{r:.2f}' for r in result['resistances']])}")
        print()
        
        # 给出介入建议
        trend_strength = result['analysis']['trend_strength']
        current_price = result['quote']['price'] if result['quote'] else result['analysis']['current_close']
        
        nearest_support = max([s for s in result['supports'] if s < current_price], default=None)
        nearest_resistance = min([r for r in result['resistances'] if r > current_price], default=None)
        
        if nearest_support:
            distance_to_support = ((current_price - nearest_support) / nearest_support) * 100
        else:
            distance_to_support = None
            
        if nearest_resistance:
            distance_to_resistance = ((nearest_resistance - current_price) / current_price) * 100
        else:
            distance_to_resistance = None
        
        print(f"- **距离最近支撑位**: {f'{distance_to_support:.2f}%' if distance_to_support else '当前已在支撑位下方'}")
        print(f"- **距离最近压力位**: {f'{distance_to_resistance:.2f}%' if distance_to_resistance else '当前已在压力位上方'}")
        print()
        
        # 介入建议
        print("**介入建议**: ")
        if trend_strength == 2:
            # 上升趋势
            if nearest_support and distance_to_support > 3:
                print("  当前处于明确上升趋势，价格远离支撑位，建议等待回踩支撑位后低吸。")
            elif nearest_support and distance_to_support <= 3:
                print("  价格回踩关键支撑位，处于上升趋势中，适合逢低介入。")
            else:
                print("  明确上升趋势，可在均线附近轻仓介入。")
        elif trend_strength == -2:
            # 下降趋势
            print("  当前处于明确下降趋势，建议观望，不建议抄底。")
        elif trend_strength == 0 and "上升趋势回调" in result['analysis']['trend']:
            if nearest_support and distance_to_support <= 5:
                print("  上升趋势回调至支撑位附近，可考虑分批低吸。")
            else:
                print("  上升趋势回调中，等待进一步回调至支撑位再介入。")
        elif trend_strength == 0 and "下降趋势反弹" in result['analysis']['trend']:
            if nearest_resistance and distance_to_resistance <= 5:
                print("  下降趋势反弹至压力位附近，建议不追高，警惕继续回调。")
            else:
                print("  下降趋势反弹，持续性有待观察，建议观望。")
        else:
            # 震荡
            if nearest_support and distance_to_support <= 3:
                print("  震荡区间下沿，可轻仓试错，止损放在支撑位下方。")
            elif nearest_resistance and distance_to_resistance <= 3:
                print("  震荡区间上沿，不建议追高，可考虑高抛。")
            else:
                print("  当前处于震荡整理，建议在支撑位和压力位之间做高抛低吸，或等待突破后再介入。")
        
        print()
        print("-" * 60)
        print()

if __name__ == "__main__":
    from pathlib import Path
    main()
