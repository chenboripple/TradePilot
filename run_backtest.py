#!/usr/bin/env python3
"""
TradePilot 回测脚本

用法：
    python3 run_backtest.py [代码] [名称] [天数]

示例：
    python3 run_backtest.py 002022.SZ 科华生物 90
    python3 run_backtest.py  # 默认回测 002022 过去 90 天
"""

import sys
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from ripple_tradePilot.backtest.runner import BacktestManager

def main():
    # 解析参数
    if len(sys.argv) > 1:
        ts_code = sys.argv[1]
    else:
        ts_code = "002022.SZ"
    
    if len(sys.argv) > 2:
        name = sys.argv[2]
    else:
        name = "科华生物"
    
    if len(sys.argv) > 3:
        days = int(sys.argv[3])
    else:
        days = 90  # 默认 3 个月
    
    print("="*70)
    print("🔍 TradePilot 回测系统")
    print("="*70)
    print(f"\n📊 回测配置:")
    print(f"   标的：{name} ({ts_code})")
    print(f"   天数：{days}天")
    print(f"   策略：MA Cross + RSI + Bollinger Bands (三策略投票)")
    print(f"   手续费：0.03%")
    print(f"   滑点：0.1%")
    print(f"   仓位：95%")
    
    # 运行回测
    manager = BacktestManager()
    result = manager.run_backtest(
        ts_code=ts_code,
        name=name,
        days=days,
        save=True,
        send_feishu=True,
    )
    
    print("\n" + "="*70)
    print("✅ 回测完成")
    print("="*70)

if __name__ == "__main__":
    main()
