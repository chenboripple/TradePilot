#!/usr/bin/env python3
"""
测试监控功能（单次检查，不循环）
"""

import sys
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.models.types import Side

# 配置
TOKEN = "your_tushare_token_here"
SYMBOL = "002022.SZ"
NAME = "科华生物"

print("="*60)
print("🔍 TradePilot 监控测试 - 单次检查")
print("="*60)

# 初始化
loader = TushareDataLoader(TOKEN)
strategy = MovingAverageCross(fast=5, slow=20)

# 获取数据
print(f"\n📈 获取 {NAME} ({SYMBOL}) 最近 60 天数据...")
bars = list(loader.load_bars(SYMBOL))
print(f"   获取到 {len(bars)} 条数据")

if not bars:
    print("❌ 无数据，退出")
    sys.exit(1)

# 预热策略
print("\n🔥 预热策略...")
for bar in bars[:-1]:
    strategy.on_bar(bar)

# 最新数据
latest_bar = bars[-1]
print(f"   最新交易日：{latest_bar.timestamp.strftime('%Y-%m-%d')}")
print(f"   收盘价：{latest_bar.close:.2f} 元")

# 生成信号
print("\n📊 生成信号...")
signal = strategy.on_bar(latest_bar)

if signal.side:
    print(f"\n🚨 交易信号：{signal.side.value}")
    print(f"   时间：{signal.timestamp}")
    print(f"   策略：{strategy.name}")
    
    if signal.side == Side.BUY:
        print("\n💡 建议操作：**买入**")
    else:
        print("\n💡 建议操作：**卖出**")
else:
    print("\n⏸️  无交易信号（持有观望）")

# 显示均线
print("\n📉 均线状态:")
fast_ma = sum(b.close for b in bars[-5:]) / 5
slow_ma = sum(b.close for b in bars[-20:]) / 20
print(f"   MA5:  {fast_ma:.2f}")
print(f"   MA20: {slow_ma:.2f}")
print(f"   差值：{fast_ma - slow_ma:.2f} ({'金叉' if fast_ma > slow_ma else '死叉'})")

print("\n" + "="*60)
print("✅ 测试完成")
print("="*60)

print("\n💡 下一步:")
print("   1. 运行正式监控：python -m ripple_tradePilot.monitor.main")
print("   2. 配置通知渠道（企业微信/钉钉）")
print("   3. 添加更多监控股票到 config.yaml")
