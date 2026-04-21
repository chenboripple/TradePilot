#!/usr/bin/env python3
"""
从全部A股中筛选符合条件的强势股票

筛选条件：
1. 近6个月涨跌幅 > 50%（涨幅强劲）
2. 近1个月涨跌幅 > 10%（近期仍在上涨）
3. 当前价格 > MA20（短期多头排列）
4. 近1个月成交量均值 > 近3个月成交量均值的80%（量能配合）

输出前20只，按近6个月涨幅排序
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../src'))

from datetime import datetime, timedelta
import pandas as pd
from typing import Optional, Tuple, List
from ripple_tradePilot.data.tushare_loader import TushareDataLoader

# Tushare Token
TOKEN = "your_tushare_token_here"

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
    max_price = recent['high'].max()
    min_price = recent['low'].min()
    
    if len(highs) < n_levels:
        if len(highs) == 0:
            highs = [max_price]
        # 添加斐波那契回撤位
        while len(highs) < n_levels:
            next_level = max_price * (0.95 - 0.05 * (len(highs)-1))
            highs.append(next_level)
        highs = sorted(highs, reverse=True)[:n_levels]
    
    if len(lows) < n_levels:
        if len(lows) == 0:
            lows = [min_price]
        while len(lows) < n_levels:
            next_level = min_price * (1.05 + 0.05 * (len(lows)-1))
            lows.append(next_level)
        lows = sorted(lows, reverse=False)[:n_levels]
    
    return sorted(lows), sorted(highs, reverse=True)

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
    
    # 需要至少6个月数据（约120交易日）
    if len(df) < 120:
        return None
    
    # 计算指标
    # 当前收盘价
    current_close = df.iloc[-1]['close']
    
    # 近6个月涨跌幅
    start_6m_idx = max(0, len(df) - 120)
    start_6m_price = df.iloc[start_6m_idx]['close']
    change_6m = ((current_close - start_6m_price) / start_6m_price) * 100
    
    # 近1个月涨跌幅（约20交易日）
    start_1m_idx = max(0, len(df) - 20)
    start_1m_price = df.iloc[start_1m_idx]['close']
    change_1m = ((current_close - start_1m_price) / start_1m_price) * 100
    
    # MA20
    ma20 = df.tail(20)['close'].mean()
    
    # 成交量判断：近1个月均值 vs 近3个月均值
    if len(df) >= 60:
        vol_1m = df.tail(20)['vol'].mean()
        vol_3m = df.tail(60)['vol'].mean()
        vol_ratio = vol_1m / vol_3m
    else:
        vol_1m = df['vol'].mean()
        vol_3m = df['vol'].mean()
        vol_ratio = 1.0
    
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
    ma60 = df.tail(60)['close'].mean() if len(df) >= 60 else df['close'].mean()
    
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
    supports, resistances = calculate_support_resistance(df)
    
    return {
        'current_close': current_close,
        'ma20': ma20,
        'ma60': ma60,
        'change_6m': change_6m,
        'change_1m': change_1m,
        'vol_ratio': vol_ratio,
        'trend': trend,
        'trend_strength': trend_strength,
        'supports': supports,
        'resistances': resistances
    }

def get_operation_suggestion(current_price: float, analysis: dict) -> str:
    """根据分析给出操作建议"""
    trend_strength = analysis['trend_strength']
    supports = analysis['supports']
    
    nearest_support = max([s for s in supports if s < current_price], default=None)
    
    if nearest_support:
        distance_to_support = ((current_price - nearest_support) / nearest_support) * 100
    else:
        distance_to_support = None
    
    change_6m = analysis['change_6m']
    change_1m = analysis['change_1m']
    
    if trend_strength == 2:
        # 多头排列
        if change_6m > 150:
            # 涨幅已经非常大
            if nearest_support and distance_to_support > 8:
                return "涨幅已大，价格远离支撑，建议等待回调"
            elif nearest_support and distance_to_support <= 8:
                return "回踩支撑位附近，可轻仓追涨，严格止损"
            else:
                return "涨幅巨大，不建议追涨，等待更好买点"
        else:
            # 涨幅适中
            if nearest_support and distance_to_support > 5:
                return "趋势良好，建议等待回调至支撑位低吸"
            elif nearest_support and distance_to_support <= 5:
                return "在支撑位附近，可逢低介入"
            else:
                return "强势上涨，可轻仓追涨"
    elif trend_strength == 0 and "上升趋势回调" in analysis['trend']:
        if nearest_support and distance_to_support <= 5:
            return "回调至支撑位，可分批低吸"
        else:
            return "仍在回调中，等待更好买点"
    else:
        if change_1m > 20:
            return "短期涨幅较大，建议观望，不追高"
        else:
            return "趋势不明确，建议观望"

def main():
    loader = TushareDataLoader(TOKEN)
    
    # 计算时间范围
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=180)).strftime('%Y%m%d')
    
    print("=" * 80)
    print(f"A 股强势股筛选")
    print(f"筛选时间: {datetime.now()}")
    print(f"开始日期: {start_date}, 结束日期: {end_date}")
    print("=" * 80)
    print()
    
    # 获取全部A股股票列表
    print("正在获取A股股票列表...")
    stock_list = loader.get_stock_list()
    print(f"共获取到 {len(stock_list)} 只上市股票")
    print()
    
    # 过滤掉北交所股票（Tushare上是 BJ 交易所，暂时不处理）
    stock_list = stock_list[~stock_list['ts_code'].str.endswith('.BJ')]
    print(f"排除北交所后，剩余 {len(stock_list)} 只股票")
    print()
    
    results = []
    processed = 0
    matched = 0
    
    # 遍历处理每只股票
    # 由于Tushare限流（50次/分钟），我们逐步处理
    for _, row in stock_list.iterrows():
        ts_code = row['ts_code']
        name = row['name']
        industry = row['industry'] if pd.notna(row['industry']) else "未知"
        
        processed += 1
        
        if processed % 100 == 0:
            print(f"已处理 {processed}/{len(stock_list)} 只，匹配到 {matched} 只...")
        
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
        
        # 获取实时价格
        quote = loader.get_realtime_quote(ts_code)
        current_price = quote['price'] if quote and quote.get('price', 0) > 0 else analysis['current_close']
        
        # 添加到结果
        matched += 1
        results.append({
            'ts_code': ts_code,
            'name': name,
            'industry': industry,
            'current_price': current_price,
            **analysis
        })
        
        # 如果已经找到20只，可以提前退出
        if len(results) >= 50:
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
    
    for i, res in enumerate(top_20, 1):
        print(f"**{i}. {res['name']} ({res['ts_code'].split('.')[0]})**")
        print()
        print(f"- 所属行业: {res['industry'] if res['industry'] else '未知'}")
        print(f"- 近6个月涨幅: {res['change_6m']:.2f}%")
        print(f"- 近1个月涨幅: {res['change_1m']:.2f}%")
        print(f"- 当前价: {res['current_price']:.2f} 元")
        
        # 第一支撑位
        supports = res['supports']
        current_price = res['current_price']
        nearest_support = max([s for s in supports if s < current_price], default=supports[0] if supports else None)
        if nearest_support:
            print(f"- 第一支撑位: {nearest_support:.2f}")
        else:
            print(f"- 第一支撑位: {supports[0]:.2f}" if supports else "无支撑位数据")
        
        print(f"- 趋势判断: {res['trend']}")
        
        # 操作建议
        suggestion = get_operation_suggestion(current_price, res)
        print(f"- 操作建议: {suggestion}")
        
        print()
        print("-" * 60)
        print()
    
    # 保存结果到CSV
    output_dir = os.path.join(os.path.dirname(__file__), '../output')
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"screen_result_{datetime.now().strftime('%Y%m%d')}.csv")
    
    # 将结果转为DataFrame保存
    output_data = []
    for res in top_20:
        supports = res['supports']
        current_price = res['current_price']
        nearest_support = max([s for s in supports if s < current_price], default=supports[0] if supports else None)
        output_data.append({
            '排名': len(output_data)+1,
            '股票代码': res['ts_code'],
            '股票名称': res['name'],
            '所属行业': res['industry'],
            '近6个月涨幅%': round(res['change_6m'], 2),
            '近1个月涨幅%': round(res['change_1m'], 2),
            '当前价': round(res['current_price'], 2),
            '第一支撑位': round(nearest_support, 2) if nearest_support else None,
            '趋势判断': res['trend'],
            '操作建议': get_operation_suggestion(current_price, res),
        })
    
    pd.DataFrame(output_data).to_csv(output_file, index=False, encoding='utf_8_sig')
    print(f"\n结果已保存到: {output_file}")

if __name__ == "__main__":
    main()
