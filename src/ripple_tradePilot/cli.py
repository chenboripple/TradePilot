"""
TradePilot 命令行工具
"""
import os
import sys
from pathlib import Path

import click

from .config_loader import load_config, init_config, get_tushare_token


@click.group()
def cli():
    """TradePilot - 波段交易系统"""
    pass


@cli.command()
@click.option('--config', '-c', type=click.Path(), help='配置文件路径')
def init(config):
    """初始化配置文件"""
    if config:
        config_path = Path(config)
    else:
        config_path = Path.home() / ".tradepilot" / "config.yaml"
    
    init_config()
    click.echo(f"✅ 配置文件已创建: {config_path}")
    click.echo("请编辑配置文件设置您的 API Key")


@cli.command()
@click.argument('symbol')
@click.option('--days', '-d', type=int, default=252, help='回测天数')
@click.option('--strategy', '-s', default='rsi', help='策略名称')
def backtest(symbol, days, strategy):
    """运行回测"""
    click.echo(f"运行回测: {symbol}, 策略: {strategy}, 天数: {days}")
    # TODO: 实现回测逻辑


@cli.command()
@click.argument('symbol')
def monitor(symbol):
    """监控股票"""
    click.echo(f"开始监控: {symbol}")
    # TODO: 实现监控逻辑


@cli.command()
def config():
    """显示当前配置"""
    import json
    cfg = load_config()
    # 隐藏敏感信息
    if 'tushare' in cfg and 'token' in cfg['tushare']:
        if cfg['tushare']['token']:
            cfg['tushare']['token'] = cfg['tushare']['token'][:8] + "..."
        else:
            cfg['tushare']['token'] = "(未设置)"
    
    click.echo(json.dumps(cfg, indent=2, ensure_ascii=False))


@cli.command()
@click.argument('symbol')
def screen(symbol):
    """筛选股票"""
    click.echo(f"筛选股票: {symbol}")
    # TODO: 实现筛选逻辑


@cli.command()
def version():
    """显示版本"""
    click.echo("TradePilot v0.1.0")


if __name__ == '__main__':
    cli()
