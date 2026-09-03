"""
TradePilot 命令行工具
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click

from . import __version__
from .config_loader import load_config, init_config, get_tushare_token


STRATEGY_BUILDERS = {
    'ma': lambda: _build('moving_average', 'MovingAverageCross'),
    'rsi': lambda: _build('rsi', 'RSI'),
    'macd': lambda: _build('macd', 'MACD'),
    'bollinger': lambda: _build('bollinger', 'BollingerBands'),
    'donchian': lambda: _build('donchian', 'DonchianBreakout'),
}


def _build(module: str, class_name: str):
    import importlib
    cls = getattr(importlib.import_module(f'ripple_tradePilot.strategies.{module}'), class_name)
    return cls()


@click.group()
def cli():
    """TradePilot - 波段交易系统"""
    pass


@cli.command()
@click.option('--config', '-c', type=click.Path(), help='配置文件路径')
def init(config):
    """初始化配置文件"""
    config_path = Path(config) if config else None
    init_config(config_path)
    target = config_path or Path.home() / ".tradepilot" / "config.yaml"
    click.echo(f"✅ 配置文件已创建: {target}")
    click.echo("请编辑配置文件设置您的 API Key")


@cli.command()
@click.argument('symbol')
@click.option('--days', '-d', type=int, default=252, help='回测天数')
@click.option('--strategy', '-s', type=click.Choice(sorted(STRATEGY_BUILDERS)), default='rsi', help='策略名称')
@click.option('--cash', type=float, default=100000.0, help='初始资金')
@click.option('--execution', type=click.Choice(['next_open', 'close']), default='next_open',
              help='撮合模式：next_open=信号次日开盘成交（默认，贴近实盘），close=当根收盘（偏乐观）')
@click.option('--benchmark', '-b', is_flag=True, help='对比沪深300基准（超额收益/相对回撤）')
@click.option('--ledger', is_flag=True, help='把本次模拟成交记入模拟盘账本（跨会话可查）')
def backtest(symbol, days, strategy, cash, execution, benchmark, ledger):
    """运行回测（统一引擎：涨跌停/100 股整数倍/佣金+印花税+滑点）"""
    from .backtest.engine import run_backtest
    from .backtest.report import compute_metrics, compute_trade_stats
    from .data.tushare_loader import TushareDataLoader

    try:
        config = load_config()
        token = get_tushare_token(config)
    except Exception as e:
        click.echo(f"❌ 无法加载配置：{e}", err=True)
        sys.exit(1)

    loader = TushareDataLoader(token, rate_limit_delay=float(config.get('tushare', {}).get('rate_limit_delay', 1.5)))
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

    click.echo(f"加载行情：{symbol} {start_date} ~ {end_date}（前复权）")
    bars = list(loader.load_bars(symbol, start_date=start_date, end_date=end_date))
    if len(bars) < 30:
        click.echo(f"❌ 行情数据不足（{len(bars)} 条），请检查 token 权限或股票代码", err=True)
        sys.exit(1)

    result = run_backtest(
        strategy=STRATEGY_BUILDERS[strategy](),
        bars=bars,
        initial_cash=cash,
        execution=execution,
    )

    metrics = compute_metrics(result.equity_curve)
    stats = compute_trade_stats(result.fills)

    click.echo(f"\n📊 回测结果（{symbol} / {strategy} / {len(bars)} 根 K 线 / 撮合={execution}）")
    click.echo(f"   总收益：{metrics.total_return:+.2%}   年化：{metrics.annual_return:+.2%}")
    click.echo(f"   最大回撤：{metrics.max_drawdown:.2%}   夏普：{metrics.sharpe:.2f}")
    click.echo(f"   交易回合：{stats.num_trades}   胜率：{stats.win_rate:.0%}   总费用：{stats.total_fees:.2f} 元")
    if benchmark:
        from .backtest.report import compare_with_benchmark
        index_df = loader.get_index_bars('000300.SH', start_date=start_date, end_date=end_date)
        if len(index_df):
            comparison = compare_with_benchmark(
                result.equity_curve, [float(v) for v in index_df['close']]
            )
            click.echo(
                f"   沪深300基准：{comparison.benchmark_return:+.2%}"
                f"   超额收益：{comparison.excess_return:+.2%}"
                f"   回撤改善：{comparison.drawdown_improvement:+.2%}"
            )
        else:
            click.echo("   ⚠️ 未取到沪深300基准数据，跳过基准对比")
    if result.halted_by_drawdown:
        click.echo("   ⚠️ 回撤闸门曾触发：其后不再开新仓")
    if result.skipped_fills:
        click.echo(f"   ⚠️ {len(result.skipped_fills)} 次委托因涨跌停无法成交")
    if ledger:
        import uuid
        from .storage.paper_ledger import record_run
        run_id = record_run(
            run_id=str(uuid.uuid4()),
            symbol=symbol,
            strategy=strategy,
            initial_cash=cash,
            final_equity=result.equity_curve[-1] if result.equity_curve else cash,
            fills=result.fills,
        )
        click.echo(f"   💾 已记入模拟盘账本：run_id={run_id}")
    click.echo("\n⚠️ 单标的样本内回测仅供参考，未经样本外验证的收益不可作为预期收益。")


_WALKFORWARD_GRIDS = {
    'ma': {'fast': [3, 5, 8], 'slow': [20, 30, 60]},
    'rsi': {'period': [7, 14, 21], 'oversold': [25, 30], 'overbought': [70, 75]},
    'bollinger': {'period': [10, 20, 30], 'std_dev': [1.5, 2.0, 2.5]},
    'donchian': {'window': [10, 20, 40]},
}


@cli.command()
@click.argument('symbol')
@click.option('--days', '-d', type=int, default=756, help='取数天数（建议 ≥2 年）')
@click.option('--strategy', '-s', type=click.Choice(sorted(_WALKFORWARD_GRIDS)), default='ma', help='策略名称')
@click.option('--splits', '-n', type=int, default=3, help='滚动分段数')
def walkforward(symbol, days, strategy, splits):
    """Walk-forward 验证：样本内选参、样本外评估，量化过拟合"""
    from .backtest.walkforward import walk_forward
    from .data.tushare_loader import TushareDataLoader

    try:
        config = load_config()
        token = get_tushare_token(config)
    except Exception as e:
        click.echo(f"❌ 无法加载配置：{e}", err=True)
        sys.exit(1)

    loader = TushareDataLoader(token)
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
    bars = list(loader.load_bars(symbol, start_date=start_date, end_date=end_date))
    if len(bars) < splits * 60:
        click.echo(f"❌ 数据不足（{len(bars)} 根 K 线），walk-forward 建议至少 {splits * 60} 根", err=True)
        sys.exit(1)

    import importlib
    module_map = {'ma': ('moving_average', 'MovingAverageCross'),
                  'rsi': ('rsi', 'RSI'),
                  'bollinger': ('bollinger', 'BollingerBands'),
                  'donchian': ('donchian', 'DonchianBreakout')}
    module_name, class_name = module_map[strategy]
    strategy_cls = getattr(importlib.import_module(f'ripple_tradePilot.strategies.{module_name}'), class_name)

    report = walk_forward(
        strategy_factory=lambda params: strategy_cls(**params),
        bars=bars,
        param_grid=_WALKFORWARD_GRIDS[strategy],
        n_splits=splits,
    )

    click.echo(f"\n📊 Walk-forward 结果（{symbol} / {strategy} / {len(bars)} 根 K 线 / {splits} 段）")
    for s in report.splits:
        click.echo(
            f"   段 {s.split_index + 1}：最佳参数 {s.best_params}"
            f"   样本内收益 {s.is_metrics.total_return:+.2%}（夏普 {s.is_metrics.sharpe:.2f}）"
            f"   样本外收益 {s.oos_metrics.total_return:+.2%}（夏普 {s.oos_metrics.sharpe:.2f}）"
        )
    click.echo(f"   样本外拼接总收益：{report.oos_total_return:+.2%}")
    click.echo(f"   平均样本内收益：{report.avg_is_return:+.2%}   平均样本外收益：{report.avg_oos_return:+.2%}")
    click.echo(f"   过拟合差距（IS−OOS）：{report.overfit_gap:+.2%}")
    if report.overfit_gap > 0.05:
        click.echo("   ⚠️ 样本内明显优于样本外：参数很可能是拟合噪音，勿按样本内收益预期。")
    else:
        click.echo("   ✅ 样本内外差距不大，但仍建议更长时间段与多标的复核。")


@cli.command()
@click.argument('symbol', required=False)
def monitor(symbol):
    """启动实时监控；传股票代码则只检查该标的并输出信号"""
    from .monitor.main import MarketMonitor, main as monitor_main

    config_path = Path(os.getenv('TRADEPILOT_CONFIG', 'config.yaml'))
    if not config_path.exists():
        config_path = Path.home() / '.tradepilot' / 'config.yaml'
    if not config_path.exists():
        click.echo("❌ 找不到配置文件：请设置 TRADEPILOT_CONFIG 或先运行 tradepilot init", err=True)
        sys.exit(1)

    if symbol:
        async def _once():
            m = MarketMonitor(str(config_path))
            results = []
            await m.check_symbol({'code': symbol.upper(), 'name': symbol, 'strategy_profile': None}, results)
            for r in results:
                click.echo(f"{r['code']} {r['name']}：{r['recommendation']} @ {r['price']}")
        asyncio.run(_once())
        return

    asyncio.run(monitor_main())


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
    """趋势筛选：均线多头排列 + 近 20 日涨幅"""
    from .data.tushare_loader import TushareDataLoader

    try:
        cfg = load_config()
        token = get_tushare_token(cfg)
    except Exception as e:
        click.echo(f"❌ 无法加载配置：{e}", err=True)
        sys.exit(1)

    loader = TushareDataLoader(token)
    bars = list(loader.load_bars(symbol, start_date=(datetime.now() - timedelta(days=90)).strftime('%Y%m%d')))
    if len(bars) < 25:
        click.echo(f"❌ 行情数据不足（{len(bars)} 条）", err=True)
        sys.exit(1)

    closes = [b.close for b in bars]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    change20 = closes[-1] / closes[-21] - 1

    click.echo(f"\n🔍 {symbol} 趋势筛选")
    click.echo(f"   最新收盘：{closes[-1]:.2f}   近 20 日涨幅：{change20:+.2%}")
    click.echo(f"   MA5={ma5:.2f}  MA10={ma10:.2f}  MA20={ma20:.2f}")
    if ma5 > ma10 > ma20:
        click.echo("   ✅ 均线多头排列（强趋势）")
    elif ma5 < ma10 < ma20:
        click.echo("   🔻 均线空头排列（弱趋势）")
    else:
        click.echo("   ➖ 均线交织（震荡）")


@cli.command()
def version():
    """显示版本"""
    click.echo(f"TradePilot v{__version__}")


if __name__ == '__main__':
    cli()
