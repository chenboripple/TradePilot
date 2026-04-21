#!/usr/bin/env python3
"""
TradePilot 安装脚本
支持 macOS、Linux、Windows
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path


def run_cmd(cmd, check=True):
    """运行命令"""
    print(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result


def install():
    """安装 TradePilot"""
    project_root = Path(__file__).parent.absolute()
    
    print("🚀 TradePilot 安装程序")
    print(f"📁 项目目录: {project_root}")
    
    # 检查 Python 版本
    py_version = sys.version_info
    if py_version < (3, 9):
        print("❌ 需要 Python 3.9 或更高版本")
        sys.exit(1)
    print(f"✅ Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    
    # 检查 pip
    if not shutil.which("pip"):
        print("❌ 未找到 pip，请先安装")
        sys.exit(1)
    print("✅ pip 已安装")
    
    # 安装依赖
    print("\n📦 安装依赖...")
    run_cmd(f"{sys.executable} -m pip install -e {project_root}")
    
    # 创建配置目录
    config_dir = Path.home() / ".tradepilot"
    config_dir.mkdir(exist_ok=True)
    print(f"✅ 配置目录: {config_dir}")
    
    # 创建配置模板
    config_file = config_dir / "config.yaml"
    if not config_file.exists():
        config_template = """# TradePilot 用户配置
# 设置环境变量或使用此配置文件

# Tushare 配置
tushare:
  token: ""  # 或设置环境变量 TUSHARE_TOKEN
  cache_dir: "data/cache"
  rate_limit_delay: 1.5

# 东方财富妙想配置（可选）
mx:
  api_key: ""  # 或设置环境变量 MX_APIKEY

# 飞书机器人配置
feishu:
  webhook_url: ""  # 或设置环境变量 FEISHU_WEBHOOK_URL
  webhook_secret: ""  # 或设置环境变量 FEISHU_WEBHOOK_SECRET

# 监控配置
monitor:
  interval_seconds: 300
  bar_freq: "1min"
  trading_hours:
    start: "09:30"
    end: "15:00"
  check_non_trading: false

# 监控标的
symbols:
  - code: "002022.SZ"
    name: "科华生物"
    strategy_profile: "rsi_002022"
    notify_on: ["BUY", "SELL"]
"""
        config_file.write_text(config_template, encoding='utf-8')
        print(f"✅ 配置文件已创建: {config_file}")
    else:
        print(f"⚠️ 配置文件已存在: {config_file}")
    
    print("\n✅ 安装完成!")
    print("\n使用方法:")
    print("  tradepilot --help        显示帮助")
    print("  tradepilot init          初始化配置")
    print("  tradepilot config        查看配置")
    print("  tradepilot backtest SYMBOL  运行回测")
    print("  tradepilot monitor SYMBOL   监控股票")
    
    print("\n配置说明:")
    print("  1. 编辑配置文件: ~/.tradepilot/config.yaml")
    print("  2. 或使用环境变量:")
    print("     export TUSHARE_TOKEN=your_token")
    print("     export FEISHU_WEBHOOK_SECRET=your_secret")


def update():
    """更新 TradePilot"""
    project_root = Path(__file__).parent.absolute()
    print("🔄 更新 TradePilot...")
    run_cmd(f"{sys.executable} -m pip install --upgrade -e {project_root}")
    print("✅ 更新完成")


def uninstall():
    """卸载 TradePilot"""
    print("🗑️  卸载 TradePilot...")
    run_cmd(f"{sys.executable} -m pip uninstall ripple_tradePilot -y")
    print("✅ 卸载完成")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='TradePilot 安装程序')
    parser.add_argument('--update', action='store_true', help='更新')
    parser.add_argument('--uninstall', action='store_true', help='卸载')
    args = parser.parse_args()
    
    if args.uninstall:
        uninstall()
    elif args.update:
        update()
    else:
        install()
