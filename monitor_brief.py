#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'src'))

from ripple_tradePilot.monitor.main import MarketMonitor
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier


def format_recommendation(rec: str) -> str:
    mapping = {
        '🟢 买入': '买入',
        '🔴 卖出': '卖出',
        '🟡 观望 (信号冲突)': '观望',
        '⚪ 观望': '观望',
        '❌ 错误': '异常',
    }
    return mapping.get(rec, rec)


def build_reason(item: dict) -> str:
    rec = item.get('recommendation', '⚪ 观望')
    buy_count = item.get('buy_count', 0)
    sell_count = item.get('sell_count', 0)
    profile = item.get('strategy_profile', '')
    fallback_note = item.get('fallback_note')
    windows = item.get('window_summary')
    if windows:
        return windows
    if rec == '🟢 买入':
        return f"{profile} 当前偏多，BUY={buy_count}/SELL={sell_count}"
    if rec == '🔴 卖出':
        return f"{profile} 当前转弱，BUY={buy_count}/SELL={sell_count}"
    if '冲突' in rec:
        return f"信号冲突，BUY={buy_count}/SELL={sell_count}"
    if fallback_note:
        return fallback_note
    if rec == '❌ 错误':
        return item.get('error', '数据或策略计算异常')
    return f"暂无明确信号，BUY={buy_count}/SELL={sell_count}"


def _run_profile(monitor: MarketMonitor, profile: dict, bars: list):
    if profile.get('kind') == 'grid_combo':
        return monitor._run_grid_combo_profile(profile, bars)
    if profile.get('kind') == 'rsi':
        return monitor._run_rsi_profile(profile, bars)
    if profile.get('kind') == 'breakout':
        return monitor._run_breakout_profile(profile, bars)
    if profile.get('kind') == 'macd':
        return monitor._run_macd_profile(profile, bars)
    return {}, None, '⚪ 观望'


def _window_vote(rec_1m: str, rec_3m: str, rec_6m: str) -> str:
    """3个月主导，1个月确认，6个月过滤。"""
    # 3个月为主信号
    if rec_3m == '🟢 买入':
        if rec_1m == '🔴 卖出':
            return '🟡 观望 (1个月逆转，暂不追多)'
        if rec_6m == '🔴 卖出':
            return '🟡 观望 (6个月逆势，先观察)'
        return '🟢 买入'

    if rec_3m == '🔴 卖出':
        if rec_1m == '🟢 买入':
            return '🟡 观望 (1个月反抽，暂不杀跌)'
        if rec_6m == '🟢 买入':
            return '🟡 观望 (6个月仍强，先观察)'
        return '🔴 卖出'

    # 3个月观望时，用1个月提供轻度偏向，但6个月做过滤
    if rec_1m == '🟢 买入' and rec_6m != '🔴 卖出':
        return '🟡 观望 (1个月偏多，等待3个月确认)'
    if rec_1m == '🔴 卖出' and rec_6m != '🟢 买入':
        return '🟡 观望 (1个月偏空，等待3个月确认)'
    return '⚪ 观望'


def apply_daily_plan(result: dict, monitor: MarketMonitor, mode: str = 'preopen') -> dict:
    """基于 1个月 + 3个月 + 6个月三个日线窗口生成日常监控建议。3个月主导，1个月确认，6个月过滤。
    修改：每个窗口拉取 (warmup_days + window_days) 数据，用 90 天做预热，只统计最后 window_days 信号。
    """
    symbol = result['code']
    try:
        profile_name = result.get('strategy_profile', '')
        profile = monitor.strategy_profiles.get(profile_name, {})

        window_days = {'1m': 30, '3m': 90, '6m': 183}
        warmup_days = 90  # 统一用 3个月数据做策略预热，确保指标计算到位
        window_results = {}
        latest = None

        for label, days in window_days.items():
            # 拉取预热+目标总共 (warmup_days + days) 天的数据
            total_days = warmup_days + days
            daily = monitor.data_loader.get_daily_bars(symbol, start_date=(datetime.now() - timedelta(days=total_days)).strftime('%Y%m%d'))
            if daily is None or len(daily) == 0:
                continue

            all_bars = []
            for _, row in daily.iterrows():
                trade_date = datetime.strptime(str(row['trade_date']), '%Y%m%d')
                all_bars.append(type('DailyBar', (), {
                    'timestamp': trade_date,
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': float(row.get('vol', 0)) * 100,
                })())

            # 只保留最后 days 天用于统计信号
            if len(all_bars) > days:
                bars = all_bars[-days:]
            else:
                bars = all_bars  # 数据不够时用全部
            latest = all_bars[-1]
            signals, strongest_signal, recommendation = _run_profile(monitor, profile, all_bars)
            buy_count = sum(1 for s in signals.values() if s.side and str(s.side) == 'BUY')
            sell_count = sum(1 for s in signals.values() if s.side and str(s.side) == 'SELL')
            window_results[label] = {
                'recommendation': recommendation,
                'buy_count': buy_count,
                'sell_count': sell_count,
                'bars': len(bars),
                'total_bars_fetched': len(all_bars),
            }

        if not window_results or latest is None:
            result['fallback_note'] = '1个月/3个月/6个月窗口日线数据不可用，暂无法生成预案'
            return result

        rec_1m = window_results.get('1m', {}).get('recommendation', '⚪ 观望')
        rec_3m = window_results.get('3m', {}).get('recommendation', '⚪ 观望')
        rec_6m = window_results.get('6m', {}).get('recommendation', '⚪ 观望')
        final_recommendation = _window_vote(rec_1m, rec_3m, rec_6m)
        buy_count = max(
            window_results.get('1m', {}).get('buy_count', 0),
            window_results.get('3m', {}).get('buy_count', 0),
            window_results.get('6m', {}).get('buy_count', 0),
        )
        sell_count = max(
            window_results.get('1m', {}).get('sell_count', 0),
            window_results.get('3m', {}).get('sell_count', 0),
            window_results.get('6m', {}).get('sell_count', 0),
        )

        trade_date_fmt = latest.timestamp.strftime('%Y-%m-%d')
        label = '盘前预案' if mode == 'preopen' else '日线复核'
        summary = (
            f"1个月：{rec_1m} (BUY={window_results.get('1m', {}).get('buy_count', 0)}/SELL={window_results.get('1m', {}).get('sell_count', 0)})；"
            f"3个月：{rec_3m} (BUY={window_results.get('3m', {}).get('buy_count', 0)}/SELL={window_results.get('3m', {}).get('sell_count', 0)})；"
            f"6个月：{rec_6m} (BUY={window_results.get('6m', {}).get('buy_count', 0)}/SELL={window_results.get('6m', {}).get('sell_count', 0)})"
        )
        result.update({
            'recommendation': final_recommendation,
            'buy_count': buy_count,
            'sell_count': sell_count,
            'price': latest.close,
            'latest_bar': latest,
            'window_results': window_results,
            'window_summary': summary,
            'fallback_note': f"按最近可用日线生成{label}；参考收盘日 {trade_date_fmt}，参考价 {latest.close:.2f}",
            'error': '',
        })
    except Exception as e:
        result['fallback_note'] = f"日线预案生成失败：{e}"
    return result


def send_feishu(text: str, monitor: MarketMonitor):
    feishu_cfg = monitor.config.get('notifiers', {}).get('feishu', {})
    if not feishu_cfg.get('enabled'):
        return False
    notifier = FeishuWebhookNotifier(feishu_cfg.get('webhook', ''), feishu_cfg.get('secret'))
    return notifier._send_text({'msg_type': 'text', 'content': {'text': text}})


def main():
    args = sys.argv[1:]
    mode = 'preopen'
    send_flag = False
    for arg in args:
        if arg in ('preopen', 'intraday'):
            mode = arg
        elif arg == '--send-feishu':
            send_flag = True

    monitor = MarketMonitor(str(Path(__file__).parent / 'config.yaml'))

    now = datetime.now()
    if not monitor.data_loader.is_trade_day(now):
        text = f"今天 {now.strftime('%Y-%m-%d')} A股不开市，今天无需操作。"
        print(text)
        if send_flag:
            send_feishu(text, monitor)
        return

    include_intraday = mode == 'intraday'
    lines = []
    title = 'TradePilot 14:30 盘中复核' if include_intraday else 'TradePilot 9:00 盘前提醒'
    lines.append(title)
    lines.append(now.strftime('时间：%Y-%m-%d %H:%M'))
    if not include_intraday:
        lines.append('说明：本预案基于最近 1个月 + 3个月 + 6个月日线窗口生成；3个月主导，1个月确认，6个月过滤。')
    else:
        lines.append('说明：本复核基于最近 1个月 + 3个月 + 6个月日线窗口生成；3个月主导，1个月确认，6个月过滤。')
    lines.append('')

    for symbol_cfg in monitor.config.get('symbols', []):
        symbol = symbol_cfg['code']
        name = symbol_cfg.get('name', symbol)
        profile_name = symbol_cfg.get('strategy_profile', '')

        result = {
            'code': symbol,
            'name': name,
            'strategy_profile': profile_name,
            'recommendation': '❌ 错误',
            'buy_count': 0,
            'sell_count': 0,
            'error': '未执行',
        }

        try:
            result = apply_daily_plan(result, monitor, mode=mode)
        except Exception as e:
            result['error'] = str(e)

        action = format_recommendation(result['recommendation'])
        reason = build_reason(result)
        lines.append(f"{name}（{symbol}）：{action}")
        if result.get('price'):
            lines.append(f"- 最新价：{result['price']:.2f}")
        if include_intraday and result.get('quote'):
            q = result['quote']
            lines.append(f"- 当日涨跌幅：{q.get('pct_change', 0):.2f}% | 涨跌额：{q.get('change', 0):.2f}")
            lines.append(f"- 成交量：{q.get('volume', 0)/10000:.1f}万股 | 成交额：{q.get('amount', 0)/100000000:.2f}亿")
        lines.append(f"- 原因：{reason}")
        if action == '观望':
            lines.append('- 建议：暂不动作')
        lines.append('')

    text = '\n'.join(lines).strip()
    print(text)
    if send_flag:
        send_feishu(text, monitor)


if __name__ == '__main__':
    main()
