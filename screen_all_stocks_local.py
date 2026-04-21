#!/usr/bin/env python3
"""
使用本地缓存股票列表，从全部A股中筛选符合条件的强势股票

筛选条件：
1. 近6个月涨跌幅 > 50%（涨幅强劲）
2. 近1个月涨跌幅 > 10%（近期仍在上涨）
3. 当前价格 > MA20（短期多头排列）
4. 近1个月成交量均值 > 近3个月成交量均值的80%（量能配合）

输出前20只，按近6个月涨幅排序
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), './src'))

from datetime import datetime, timedelta
import pandas as pd
from pathlib import Path
from typing import Optional, Tuple, List
from ripple_tradePilot.data.tushare_loader import TushareDataLoader

# Tushare Token
TOKEN = "3900cd9a9e8ec10fc1249e98ea6d7a7eb88c8b9a2131e268f20efec4"

def calculate_support_resistance(df: pd.DataFrame, n_levels: int = 3) -> list:
    """
    根据近期高点低点计算支撑位，返回从高到低排序的支撑位列表（第一个就是第一支撑位）
    """
    # 按时间排序
    df = df.sort_values('trade_date', ascending=True).reset_index(drop=True)
    
    # 获取最近6个月的数据点
    recent = df.tail(120)
    
    # 找局部低点（支撑位）
    lows = []
    for i in range(2, len(recent) - 2):
        if recent.iloc[i].low < recent.iloc[i-1].low and recent.iloc[i].low < recent.iloc[i-2].low and \
           recent.iloc[i].low < recent.iloc[i+1].low and recent.iloc[i].low < recent.iloc[i+2].low:
            lows.append(recent.iloc[i].low)
    
    # 当前价格
    current_price = recent.iloc[-1].close
    
    # 只保留低于当前价格的支撑位，从高到低排序
    lows_below = [l for l in lows if l < current_price]
    lows_below = sorted(lows_below, reverse=True)
    
    # 如果数量不够，用百分比补充
    if len(lows_below) == 0:
        min_price = recent['low'].min()
        lows_below = [min_price]
    
    while len(lows_below) < n_levels:
        next_support = lows_below[-1] * 0.95
        if next_support not in lows_below:
            lows_below.append(next_support)
    
    return lows_below[:n_levels]

def analyze_stock(df: pd.DataFrame) -> Optional[dict]:
    """
    分析股票是否符合筛选条件，并返回关键指标
    
    筛选条件：
    1. 近6个月涨跌幅 > 50%
    2. 近1个月涨跌幅 > 10%
    3. 当前价格 > MA20
    4. 近1个月成交量均值 > 近3个月成交量均值的80%
    """
    df = df.sort_values('trade_date', ascending=True).reset_index(drop=True)
    
    # 需要至少120个交易日数据（约6个月）
    if len(df) < 120:
        return None
    
    # 当前收盘价
    current_close = df.iloc[-1]['close']
    
    # 近6个月涨跌幅（从120天前算起）
    start_6m_idx = len(df) - 120
    start_6m_price = df.iloc[start_6m_idx]['close']
    change_6m = ((current_close - start_6m_price) / start_6m_price) * 100
    
    # 近1个月涨跌幅（约20交易日）
    start_1m_idx = max(0, len(df) - 20)
    start_1m_price = df.iloc[start_1m_idx]['close']
    change_1m = ((current_close - start_1m_price) / start_1m_price) * 100
    
    # MA20
    df['ma20'] = df['close'].rolling(window=20).mean()
    ma20 = df.iloc[-1]['ma20']
    
    # 成交量判断：近1个月均值 vs 近3个月均值
    vol_1m = df.tail(20)['vol'].mean()
    vol_3m = df.tail(60)['vol'].mean()
    vol_ratio = vol_1m / vol_3m if vol_3m > 0 else 1.0
    
    # 筛选条件检查
    if change_6m <= 50:
        return None
    if change_1m <= 10:
        return None
    if current_close <= ma20:
        return None
    if vol_ratio <= 0.8:
        return None
    
    # MA60 用于趋势判断
    df['ma60'] = df['close'].rolling(window=60).mean()
    ma60 = df.iloc[-1]['ma60']
    
    # 判断趋势
    if current_close > ma20 > ma60:
        trend = "多头排列，强势上涨"
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
    
    # 计算支撑位
    supports = calculate_support_resistance(df)
    
    return {
        'current_close': current_close,
        'ma20': ma20,
        'ma60': ma60,
        'change_6m': change_6m,
        'change_1m': change_1m,
        'vol_ratio': vol_ratio,
        'trend': trend,
        'trend_strength': trend_strength,
        'supports': supports
    }

def get_operation_suggestion(current_price: float, analysis: dict) -> str:
    """根据分析给出操作建议"""
    trend_strength = analysis['trend_strength']
    supports = analysis['supports']
    change_6m = analysis['change_6m']
    change_1m = analysis['change_1m']
    
    nearest_support = supports[0] if supports else None
    
    if nearest_support:
        distance_to_support = ((current_price - nearest_support) / nearest_support) * 100
    else:
        distance_to_support = None
    
    if trend_strength == 2:
        # 多头排列
        if change_6m > 150:
            # 涨幅已经非常大
            if nearest_support and distance_to_support > 8:
                return "等回调"
            elif nearest_support and distance_to_support <= 8:
                return "可轻仓追涨"
            else:
                return "等回调"
        else:
            # 涨幅适中
            if nearest_support and distance_to_support > 5:
                return "等回调"
            elif nearest_support and distance_to_support <= 5:
                return "可介入，轻仓追涨"
            else:
                return "可轻仓追涨"
    elif trend_strength == 0 and "上升趋势回调" in analysis['trend']:
        if nearest_support and distance_to_support <= 5:
            return "回调到位，可低吸"
        else:
            return "等待回调"
    else:
        if change_1m > 20:
            return "短期涨幅大，建议观望"
        else:
            return "趋势不明，建议观望"

def main():
    loader = TushareDataLoader(TOKEN)
    
    # 计算时间范围
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
    
    print("=" * 80)
    print(f"A 股强势股筛选（使用本地股票列表）")
    print(f"筛选时间: {datetime.now()}")
    print(f"开始日期: {start_date}, 结束日期: {end_date}")
    print("=" * 80)
    print()
    
    # 从本地CSV读取股票列表
    stock_list_file = "/Users/ripple/work space/ripple_tradePilot/data/stock_list.csv"
    print(f"从 {stock_list_file} 读取股票列表...")
    stock_df = pd.read_csv(stock_list_file)
    print(f"共读取 {len(stock_df)} 只股票")
    print()
    
    results = []
    processed = 0
    matched = 0
    
    # 遍历处理每只股票
    # 由于Tushare限流（50次/分钟 = ~0.8次/秒），脚本已经内置限流，会自动控制请求速度
    for _, row in stock_df.iterrows():
        ts_code = row['ts_code']
        name = row['name']
        industry = row.get('industry', '未知')
        
        processed += 1
        
        if processed % 50 == 0:
            print(f"已处理 {processed}/{len(stock_df)} 只，匹配到 {matched} 只...")
        
        # 获取日线数据
        try:
            df = loader.get_daily_bars(ts_code, start_date=start_date, end_date=end_date)
        except Exception as e:
            print(f"  ❌ 获取 {name} ({ts_code}) 数据失败: {e}")
            continue
        
        if df is None or len(df) < 100:
            continue
        
        # 分析股票
        analysis = analyze_stock(df)
        if analysis is None:
            continue
        
        # 获取实时价格（AkShare获取更快）
        quote = loader.get_realtime_quote(ts_code)
        current_price = quote['price'] if quote and quote.get('price', 0) > 0 else analysis['current_close']
        
        # 添加到结果
        matched += 1
        results.append({
            'ts_code': ts_code,
            'name': name,
            'industry': str(industry),
            'current_price': current_price,
            **analysis
        })
        
        # 如果已经找到足够多的，停止筛选
        if len(results) >= 30:
            print(f"已找到 {len(results)} 只符合条件股票，停止筛选...")
            break
    
    print()
    print(f"筛选完成，共找到 {len(results)} 只符合条件股票")
    print()
    
    # 按近6个月涨幅排序，取前20
    results_sorted = sorted(results, key=lambda x: x['change_6m'], reverse=True)
    top_20 = results_sorted[:20]
    
    # 输出结果
    print("=" * 80)
    print(f"前20只符合条件股票（按近6个月涨幅降序）")
    print("=" * 80)
    print()
    
    output_data = []
    for i, res in enumerate(top_20, 1):
        code_only = res['ts_code'].split('.')[0]
        print(f"**{i}. {res['name']} ({code_only})**")
        print()
        print(f"- 股票代码和名称: {code_only} {res['name']}")
        print(f"- 所属行业: {res['industry'] if res['industry'] != 'nan' else '未知'}")
        print(f"- 近6个月涨幅: {res['change_6m']:.2f}%")
        print(f"- 近1个月涨幅: {res['change_1m']:.2f}%")
        print(f"- 当前价: {res['current_price']:.2f} 元")
        
        # 第一支撑位
        supports = res['supports']
        if supports:
            first_support = supports[0]
            print(f"- 第一支撑位: {first_support:.2f}")
        else:
            first_support = None
            print(f"- 第一支撑位: 无数据")
        
        print(f"- 趋势判断: {res['trend']}")
        
        # 操作建议
        suggestion = get_operation_suggestion(res['current_price'], res)
        print(f"- 操作建议: {suggestion}")
        
        print()
        print("-" * 60)
        print()
        
        # 保存用于CSV
        output_data.append({
            '排名': i,
            '股票代码': code_only,
            '股票名称': res['name'],
            '所属行业': res['industry'] if res['industry'] != 'nan' else '未知',
            '近6个月涨幅%': round(res['change_6m'], 2),
            '近1个月涨幅%': round(res['change_1m'], 2),
            '当前价': round(res['current_price'], 2),
            '第一支撑位': round(first_support, 2) if first_support else None,
            '趋势判断': res['trend'],
            '操作建议': suggestion
        })
    
    # 保存结果到CSV
    output_dir = Path("/Users/ripple/work space/ripple_tradePilot/output")
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"screen_top20_{datetime.now().strftime('%Y%m%d')}.csv"
    
    pd.DataFrame(output_data).to_csv(output_file, index=False, encoding='utf_8_sig')
    print(f"\n完整结果已保存到: {output_file}")

if __name__ == "__main__":
    main()
