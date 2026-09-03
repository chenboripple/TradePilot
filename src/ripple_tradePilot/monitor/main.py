"""
TradePilot 实时监控主程序
功能：轮询行情 → 策略分析 → 信号通知
"""

import asyncio
import logging
import os
import signal as sys_signal
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Tuple

from ripple_tradePilot.config_loader import load_config
from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.strategies.macd import MACD
from ripple_tradePilot.strategies.combo_vote import ComboVoteStrategy
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier
from ripple_tradePilot.models.types import Bar, Side, Signal
from ripple_tradePilot.storage.user_store import get_system_strategy


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("TradePilot")


class SignalNotifier:
    """信号通知器"""
    
    def __init__(self, config: dict):
        self.config = config
        self._last_signals = {}  # 避免重复通知：{symbol: (side, timestamp)}
    
    def should_notify(self, symbol: str, side: Side) -> bool:
        """检查是否应该发送通知（1 小时内不重复）"""
        key = f"{symbol}:{side.value}"
        if key in self._last_signals:
            last_time = self._last_signals[key]
            if (datetime.now() - last_time).total_seconds() < 3600:
                return False
        return True
    
    def record(self, symbol: str, side: Side):
        """记录已通知的信号"""
        key = f"{symbol}:{side.value}"
        self._last_signals[key] = datetime.now()
    
    def send(self, symbol: str, name: str, side: Side, price: float, strategy: str, bar: Bar):
        """发送通知"""
        message = f"""
🚨 交易信号提醒
━━━━━━━━━━━━━━━━
📊 标的：{name} ({symbol})
📈 信号：{side.value}
💰 价格：{price:.2f} 元
📉 策略：{strategy}
⏰ 时间：{bar.timestamp.strftime('%Y-%m-%d')}
━━━━━━━━━━━━━━━━

📝 信号详情：
• 开盘：{bar.open:.2f}
• 最高：{bar.high:.2f}
• 最低：{bar.low:.2f}
• 收盘：{bar.close:.2f}
• 成交量：{bar.volume/10000:.1f}万手

⚠️ 风险提示：
• 本信号仅供参考
• 请自行判断是否交易
• 投资有风险，入市需谨慎
━━━━━━━━━━━━━━━━
"""
        
        # 控制台输出
        if self.config.get('console', {}).get('enabled', True):
            print(message)
            logger.info(f"信号：{symbol} {side.value} @ {price:.2f}")
        
        # 企业微信通知（可选）
        if self.config.get('wechat', {}).get('enabled', False):
            self._send_wechat(symbol, name, side, price, strategy, bar)
        
        # 钉钉通知（可选）
        if self.config.get('dingtalk', {}).get('enabled', False):
            self._send_dingtalk(symbol, name, side, price, strategy, bar)
    
    def _send_wechat(self, symbol: str, name: str, side: Side, price: float, strategy: str, bar: Bar):
        """发送企业微信通知"""
        import httpx
        
        webhook = self.config['wechat']['webhook']
        color = "warning" if side == Side.BUY else "comment"
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"""## 🚨 交易信号提醒

> 📊 标的：{name} ({symbol})
> 📈 信号：**{side.value}**
> 💰 价格：{price:.2f} 元
> 📉 策略：{strategy}

**信号详情：**
- 开盘：{bar.open:.2f}
- 最高：{bar.high:.2f}
- 最低：{bar.low:.2f}
- 收盘：{bar.close:.2f}

⚠️ 投资有风险，入市需谨慎"""
            }
        }
        
        try:
            response = httpx.post(webhook, json=payload, timeout=10)
            if response.status_code == 200:
                logger.info(f"企业微信通知发送成功")
            else:
                logger.error(f"企业微信通知失败：{response.text}")
        except Exception as e:
            logger.error(f"发送企业微信通知异常：{e}")
    
    def _send_dingtalk(self, symbol: str, name: str, side: Side, price: float, strategy: str, bar: Bar):
        """发送钉钉通知"""
        import httpx
        
        webhook = self.config['dingtalk']['webhook']
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"交易信号：{name}",
                "text": f"""## 🚨 交易信号提醒

> 📊 标的：{name} ({symbol})
> 📈 信号：**{side.value}**
> 💰 价格：{price:.2f} 元
> 📉 策略：{strategy}

**信号详情：**
- 开盘：{bar.open:.2f}
- 最高：{bar.high:.2f}
- 最低：{bar.low:.2f}
- 收盘：{bar.close:.2f}

⚠️ 投资有风险，入市需谨慎"""
            }
        }
        
        try:
            response = httpx.post(webhook, json=payload, timeout=10)
            result = response.json()
            if result.get('errcode') == 0:
                logger.info(f"钉钉通知发送成功")
            else:
                logger.error(f"钉钉通知失败：{result}")
        except Exception as e:
            logger.error(f"发送钉钉通知异常：{e}")


class MarketMonitor:
    """市场行情监控器"""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        
        # 初始化数据加载器
        ts_token = self.config['tushare']['token']
        rate_limit = self.config.get('tushare', {}).get('rate_limit_delay', 1.5)
        self.data_loader = TushareDataLoader(ts_token, rate_limit_delay=rate_limit)
        
        # 初始化通知器
        self.notifier = SignalNotifier(self.config.get('notifiers', {}))
        
        # 初始化飞书通知器（用于定期报告）
        feishu_config = self.config.get('notifiers', {}).get('feishu', {})
        if feishu_config.get('enabled', False):
            self.feishu_notifier = FeishuWebhookNotifier(
                webhook_url=feishu_config.get('webhook', ''),
                secret=feishu_config.get('secret')
            )
            logger.info("✅ 飞书通知器已初始化")
        else:
            self.feishu_notifier = None
        
        # 初始化按标的的策略画像
        self._configured_strategy_profiles = self.config.get('strategy_profiles', {})
        self.strategy_profiles = {}
        self._refresh_strategy_profiles()
        logger.info(f"✅ 已加载策略画像：{', '.join(sorted(self.strategy_profiles.keys()))}")
        
        # 监控状态
        self._running = False
        self._check_interval = self.config.get('monitor', {}).get('interval_seconds', 300)
        self._check_count = 0
        self._minute_freq = self.config.get('monitor', {}).get('bar_freq', '1min')

    def _refresh_strategy_profiles(self):
        self.strategy_profiles = {
            name: dict(profile)
            for name, profile in self._configured_strategy_profiles.items()
        }
        for symbol in self.config.get('symbols', []):
            profile_name = symbol.get('strategy_profile')
            if not profile_name:
                continue
            override = get_system_strategy(symbol['code'], 'stock')
            if override is None:
                continue
            configured = self.strategy_profiles.get(profile_name, {})
            self.strategy_profiles[profile_name] = {
                **override['parameters'],
                'kind': configured.get('kind', 'combo_vote'),
            }
    
    def is_trading_time(self) -> bool:
        """判断是否在 A 股交易时间。"""
        now = datetime.now()
        if not self.data_loader.is_trade_day(now):
            return False

        current = now.time()
        trading_config = self.config.get('monitor', {}).get('trading_hours', {})
        start = time.fromisoformat(trading_config.get('start', '09:30'))
        end = time.fromisoformat(trading_config.get('end', '15:00'))

        in_day_session = start <= current <= end
        in_lunch_break = time(11, 30) < current < time(13, 0)
        return in_day_session and not in_lunch_break
    
    def _run_combo_vote_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        """运行 combo_vote 策略画像（MA + RSI + BB 投票）"""
        ma_fast = profile.get('ma_fast', 5)
        ma_slow = profile.get('ma_slow', 20)
        rsi_period = profile.get('rsi_period', 14)
        rsi_oversold = profile.get('rsi_oversold', 30)
        rsi_overbought = profile.get('rsi_overbought', 70)
        bb_period = profile.get('bb_period', 20)
        bb_std = profile.get('bb_std', 2.0)
        vote_threshold = profile.get('vote_threshold', 2)

        ma_strategy = MovingAverageCross(fast=ma_fast, slow=ma_slow)
        rsi_strategy = RSI(period=rsi_period, oversold=rsi_oversold, overbought=rsi_overbought)
        bb_strategy = BollingerBands(period=bb_period, std_dev=bb_std)

        latest_bar = bars[-1]
        strategies = {
            'ma_cross': ma_strategy,
            'rsi': rsi_strategy,
            'bollinger': bb_strategy,
        }
        signals = {}
        for strategy_name, strategy in strategies.items():
            strategy.reset()
            for bar in bars[:-1]:
                strategy.on_bar(bar)
            signals[strategy_name] = strategy.on_bar(latest_bar)

        strongest_signal = None
        max_strength = 0.0
        for strategy_name, signal in signals.items():
            if signal.side and signal.strength > max_strength:
                strongest_signal = (strategy_name, signal)
                max_strength = signal.strength

        buy_count = sum(1 for s in signals.values() if s.side == Side.BUY)
        sell_count = sum(1 for s in signals.values() if s.side == Side.SELL)
        if buy_count >= vote_threshold:
            recommendation = '🟢 买入'
        elif sell_count >= vote_threshold:
            recommendation = '🔴 卖出'
        elif buy_count >= 1 or sell_count >= 1:
            recommendation = f'🟡 观望 (信号冲突，需{vote_threshold}票)'
        else:
            recommendation = '⚪ 观望'

        return signals, strongest_signal, recommendation

    def _run_grid_combo_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        ma_cfg = profile.get('ma', {})
        rsi_cfg = profile.get('rsi', {})
        bb_cfg = profile.get('bb', {})
        vote_threshold = profile.get('vote_threshold', 2)  # 默认 2 票，可配置

        ma_strategy = MovingAverageCross(fast=ma_cfg.get('fast', 5), slow=ma_cfg.get('slow', 20))
        rsi_strategy = RSI(
            period=rsi_cfg.get('period', 14),
            oversold=rsi_cfg.get('oversold', 30),
            overbought=rsi_cfg.get('overbought', 70),
        )
        bb_strategy = BollingerBands(period=bb_cfg.get('period', 20), std_dev=bb_cfg.get('std_dev', 2.0))

        latest_bar = bars[-1]
        strategies = {
            'ma_cross': ma_strategy,
            'rsi': rsi_strategy,
            'bollinger': bb_strategy,
        }
        signals = {}
        for strategy_name, strategy in strategies.items():
            strategy.reset()
            for bar in bars[:-1]:
                strategy.on_bar(bar)
            signals[strategy_name] = strategy.on_bar(latest_bar)

        strongest_signal = None
        max_strength = 0.0
        for strategy_name, signal in signals.items():
            if signal.side and signal.strength > max_strength:
                strongest_signal = (strategy_name, signal)
                max_strength = signal.strength

        buy_count = sum(1 for s in signals.values() if s.side == Side.BUY)
        sell_count = sum(1 for s in signals.values() if s.side == Side.SELL)
        if buy_count >= vote_threshold:
            recommendation = '🟢 买入'
        elif sell_count >= vote_threshold:
            recommendation = '🔴 卖出'
        elif buy_count >= 1 or sell_count >= 1:
            recommendation = f'🟡 观望 (信号冲突，需{vote_threshold}票)'
        else:
            recommendation = '⚪ 观望'

        return signals, strongest_signal, recommendation

    def _run_rsi_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        """运行纯 RSI 策略"""
        rsi_cfg = profile.get('rsi', {})
        rsi_strategy = RSI(
            period=rsi_cfg.get('period', 14),
            oversold=rsi_cfg.get('oversold', 30),
            overbought=rsi_cfg.get('overbought', 70),
        )
        
        latest_bar = bars[-1]
        rsi_strategy.reset()
        for bar in bars[:-1]:
            rsi_strategy.on_bar(bar)
        rsi_signal = rsi_strategy.on_bar(latest_bar)
        
        signals = {'rsi': rsi_signal}
        strongest_signal = ('rsi', rsi_signal) if rsi_signal.side else None
        
        if rsi_signal.side == Side.BUY:
            recommendation = '🟢 买入'
        elif rsi_signal.side == Side.SELL:
            recommendation = '🔴 卖出'
        else:
            recommendation = '⚪ 观望'
        
        return signals, strongest_signal, recommendation

    def _run_breakout_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        window = profile.get('breakout_window', 20)
        rsi_period = profile.get('rsi_period', 6)
        buy_rsi_min = profile.get('buy_rsi_min', 55)
        exit_ma = profile.get('exit_ma', 10)
        sell_rsi_max = profile.get('sell_rsi_max', 45)

        latest_bar = bars[-1]
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        highest_prev = max(highs[-(window + 1):-1]) if len(highs) > window else None
        ma_exit = sum(closes[-exit_ma:]) / exit_ma if len(closes) >= exit_ma else None

        rsi_strategy = RSI(period=rsi_period, oversold=30, overbought=70)
        for bar in bars[:-1]:
            rsi_strategy.on_bar(bar)
        rsi_signal = rsi_strategy.on_bar(latest_bar)
        latest_rsi = None
        try:
            latest_rsi = rsi_strategy._last_rsi
        except Exception:
            latest_rsi = None

        side = None
        strength = 0.0
        if highest_prev is not None and latest_rsi is not None:
            if latest_bar.close > highest_prev and latest_rsi > buy_rsi_min:
                side = Side.BUY
                strength = 1.0 + min((latest_rsi - buy_rsi_min) / 20.0, 0.5)
            elif ma_exit is not None and (latest_bar.close < ma_exit or latest_rsi < sell_rsi_max):
                side = Side.SELL
                strength = 1.0

        signal = Signal(timestamp=latest_bar.timestamp, side=side, strength=strength)
        signals = {'breakout': signal, 'rsi_filter': rsi_signal}
        strongest_signal = ('breakout', signal) if signal.side else None
        recommendation = '🟢 买入' if signal.side == Side.BUY else '🔴 卖出' if signal.side == Side.SELL else '⚪ 观望'
        return signals, strongest_signal, recommendation

    def _run_macd_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        """运行 MACD 策略"""
        macd_cfg = profile.get('macd', {})
        macd_strategy = MACD(
            fast=macd_cfg.get('fast', 12),
            slow=macd_cfg.get('slow', 26),
            signal=macd_cfg.get('signal', 9),
            zero_cross=macd_cfg.get('zero_cross', False),
        )
        
        latest_bar = bars[-1]
        macd_strategy.reset()
        for bar in bars[:-1]:
            macd_strategy.on_bar(bar)
        macd_signal = macd_strategy.on_bar(latest_bar)
        
        signals = {'macd': macd_signal}
        strongest_signal = ('macd', macd_signal) if macd_signal.side else None
        
        if macd_signal.side == Side.BUY:
            recommendation = '🟢 买入'
        elif macd_signal.side == Side.SELL:
            recommendation = '🔴 卖出'
        else:
            recommendation = '⚪ 观望'
        
        return signals, strongest_signal, recommendation

    def _run_ma_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        """运行 MA 交叉策略"""
        ma_cfg = profile.get('ma', {})
        ma_strategy = MovingAverageCross(
            fast=ma_cfg.get('fast', 5),
            slow=ma_cfg.get('slow', 20),
        )
        
        latest_bar = bars[-1]
        ma_strategy.reset()
        for bar in bars[:-1]:
            ma_strategy.on_bar(bar)
        ma_signal = ma_strategy.on_bar(latest_bar)
        
        signals = {'ma': ma_signal}
        strongest_signal = ('ma', ma_signal) if ma_signal.side else None
        
        if ma_signal.side == Side.BUY:
            recommendation = '🟢 买入'
        elif ma_signal.side == Side.SELL:
            recommendation = '🔴 卖出'
        else:
            recommendation = '⚪ 观望'
        
        return signals, strongest_signal, recommendation

    def _build_combo_components(self, profile: dict):
        components = []
        for component in profile.get('components', []):
            kind = component.get('kind')
            name = component.get('name', kind or 'component')

            if kind == 'ma':
                ma_cfg = component.get('ma', {})
                strategy = MovingAverageCross(
                    fast=ma_cfg.get('fast', 5),
                    slow=ma_cfg.get('slow', 20),
                )
            elif kind == 'rsi':
                rsi_cfg = component.get('rsi', {})
                strategy = RSI(
                    period=rsi_cfg.get('period', 14),
                    oversold=rsi_cfg.get('oversold', 30),
                    overbought=rsi_cfg.get('overbought', 70),
                )
            elif kind == 'bollinger':
                bb_cfg = component.get('bb', {})
                strategy = BollingerBands(
                    period=bb_cfg.get('period', 20),
                    std_dev=bb_cfg.get('std_dev', 2.0),
                )
            elif kind == 'macd':
                macd_cfg = component.get('macd', {})
                strategy = MACD(
                    fast=macd_cfg.get('fast', 12),
                    slow=macd_cfg.get('slow', 26),
                    signal=macd_cfg.get('signal', 9),
                    zero_cross=macd_cfg.get('zero_cross', False),
                )
            else:
                raise ValueError(f'unsupported combo component kind: {kind}')

            components.append((name, strategy))
        return components

    def _run_combo_profile(self, profile: dict, bars: List[Bar]) -> Tuple[Dict, Optional[Tuple[str, Signal]], str]:
        components = self._build_combo_components(profile)
        vote_threshold = profile.get('vote_threshold', 1)
        combo = ComboVoteStrategy(components, vote_threshold=vote_threshold)

        latest_bar = bars[-1]
        combo.reset()
        for bar in bars[:-1]:
            combo.on_bar(bar)
        signals = combo.get_component_signals(latest_bar)

        strongest_signal = None
        max_strength = 0.0
        for strategy_name, signal in signals.items():
            if signal.side and signal.strength > max_strength:
                strongest_signal = (strategy_name, signal)
                max_strength = signal.strength

        buy_count = sum(1 for s in signals.values() if s.side == Side.BUY)
        sell_count = sum(1 for s in signals.values() if s.side == Side.SELL)
        if buy_count >= vote_threshold and sell_count == 0:
            recommendation = '🟢 买入'
        elif sell_count >= vote_threshold and buy_count == 0:
            recommendation = '🔴 卖出'
        elif buy_count >= 1 or sell_count >= 1:
            recommendation = f'🟡 观望 (信号冲突，需{vote_threshold}票)'
        else:
            recommendation = '⚪ 观望'

        return signals, strongest_signal, recommendation

    async def check_symbol(self, symbol_config: dict, results: list = None):
        """检查单个标的（按股票使用专属策略画像）"""
        symbol = symbol_config['code']
        name = symbol_config.get('name', symbol)
        profile_name = symbol_config.get('strategy_profile')
        
        try:
            # 获取分钟级数据（默认 1min），用于实时监控
            end_dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            start_dt = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
            bars = list(self.data_loader.load_minute_bars(symbol, start_dt, end_dt, freq=self._minute_freq))
            if len(bars) < 30:
                logger.warning(f"分钟线数据不足：{symbol} ({len(bars)}条)")
                return
            
            latest_bar = bars[-1]
            logger.debug(f"检查 {symbol}，最新分钟价：{latest_bar.close:.2f} @ {latest_bar.timestamp}")

            profile = self.strategy_profiles.get(profile_name, {})
            profile_kind = profile.get('kind')
            if profile_kind == 'grid_combo':
                signals, strongest_signal, recommendation = self._run_grid_combo_profile(profile, bars)
            elif profile_kind == 'rsi':
                signals, strongest_signal, recommendation = self._run_rsi_profile(profile, bars)
            elif profile_kind == 'breakout':
                signals, strongest_signal, recommendation = self._run_breakout_profile(profile, bars)
            elif profile_kind == 'macd':
                signals, strongest_signal, recommendation = self._run_macd_profile(profile, bars)
            elif profile_kind == 'ma':
                signals, strongest_signal, recommendation = self._run_ma_profile(profile, bars)
            elif profile_kind == 'combo_vote':
                signals, strongest_signal, recommendation = self._run_combo_profile(profile, bars)
            else:
                logger.warning(f"未找到策略画像：{symbol} -> {profile_name} (kind={profile_kind})")
                signals, strongest_signal, recommendation = {}, None, '❌ 错误'
            
            # 发送通知逻辑（仅当有交易信号时）
            if strongest_signal:
                strategy_name, signal = strongest_signal
                # 检查是否应该通知
                if self.notifier.should_notify(symbol, signal.side):
                    self.notifier.send(
                        symbol=symbol,
                        name=name,
                        side=signal.side,
                        price=latest_bar.close,
                        strategy=f"{profile_name}:{strategy_name}",
                        bar=latest_bar
                    )
                    self.notifier.record(symbol, signal.side)
                else:
                    logger.info(f"跳过重复通知：{symbol} {signal.side.value}")
            
            buy_count = sum(1 for s in signals.values() if s.side == Side.BUY)
            sell_count = sum(1 for s in signals.values() if s.side == Side.SELL)

            # 记录结果（用于定期报告）
            if results is not None:
                results.append({
                    'code': symbol,
                    'name': name,
                    'price': latest_bar.close,
                    'recommendation': recommendation,
                    'buy_count': buy_count,
                    'sell_count': sell_count,
                })
            
            # 记录多策略状态
            logger.info(
                f"{symbol} 信号：BUY={buy_count}/SELL={sell_count} "
                f"{'🟢看涨' if buy_count >= 2 else '🔴看跌' if sell_count >= 2 else '⚪观望'}"
            )
        
        except Exception as e:
            logger.error(f"检查 {symbol} 失败：{e}", exc_info=True)
            if results is not None:
                results.append({
                    'code': symbol,
                    'name': name,
                    'price': 0,
                    'recommendation': '❌ 错误',
                    'buy_count': 0,
                    'sell_count': 0,
                })
    
    async def send_periodic_report(self, results: list):
        """发送定期监控报告到飞书"""
        if not self.feishu_notifier:
            return
        
        from datetime import datetime
        
        # 构建报告内容
        buy_stocks = [r for r in results if r.get('recommendation') == '🟢 买入']
        sell_stocks = [r for r in results if r.get('recommendation') == '🔴 卖出']
        hold_stocks = [r for r in results if '观望' in r.get('recommendation', '')]
        
        # 构建消息内容
        content = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "📊 TradePilot 监控报告"},
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**监控时间：** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n**监控标的：** {len(results)}只\n**检查次数：** {self._check_count}"
                        }
                    },
                    {
                        "tag": "hr"
                    }
                ]
            }
        }
        
        # 添加重点信号
        if buy_stocks:
            buy_text = "\n".join([f"• {r['name']} ({r['code']}) - {r['price']:.2f}元" for r in buy_stocks])
            content["card"]["elements"].append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"🟢 **买入信号** ({len(buy_stocks)}只):\n{buy_text}"
                }
            })
        
        if sell_stocks:
            sell_text = "\n".join([f"• {r['name']} ({r['code']}) - {r['price']:.2f}元" for r in sell_stocks])
            content["card"]["elements"].append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"🔴 **卖出信号** ({len(sell_stocks)}只):\n{sell_text}"
                }
            })
        
        # 观望股票摘要
        if hold_stocks:
            hold_summary = f"⚪ 观望：{len(hold_stocks)}只（无明确信号）"
            content["card"]["elements"].append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": hold_summary
                }
            })
        
        # 添加备注
        content["card"]["elements"].append({
            "tag": "hr"
        })
        content["card"]["elements"].append({
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "⚠️ 投资有风险，入市需谨慎。本监控仅供参考，不构成投资建议。"
                }
            ]
        })
        
        # 发送消息
        try:
            # 使用简单文本消息代替卡片（更可靠）
            text_content = {
                "msg_type": "text",
                "content": {
                    "text": f"📊 TradePilot 监控报告\n"
                            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                            f"标的：{len(results)}只 | 检查：{self._check_count}次\n\n"
                            f"🟢 买入：{len(buy_stocks)}只\n"
                            f"🔴 卖出：{len(sell_stocks)}只\n"
                            f"⚪ 观望：{len(hold_stocks)}只\n\n"
                            f"{'✅ 无异常信号' if not buy_stocks and not sell_stocks else '⚠️ 有交易信号，请查看'}"
                }
            }
            
            self.feishu_notifier._send_text(text_content)
            logger.info("✅ 飞书定期报告已发送")
        except Exception as e:
            logger.error(f"发送飞书报告失败：{e}")
    
    async def run_loop(self):
        """主监控循环"""
        logger.info("🚀 TradePilot 启动监控...")
        logger.info(f"📊 监控标的数：{len(self.config.get('symbols', []))}")
        logger.info(f"⏱️ 检查间隔：{self._check_interval}秒")
        logger.info(f"🕒 交易时间：09:30-15:00（仅 A 股开市时间）")
        logger.info(f"📱 飞书通知：{'✅ 已启用' if self.feishu_notifier else '❌ 未启用'}")
        
        self._running = True
        
        while self._running:
            results = []  # 存储本次检查结果
            
            try:
                # 检查是否交易时间
                if self.is_trading_time():
                    self._refresh_strategy_profiles()
                    self._check_count += 1
                    logger.info(f"\n{'='*60}")
                    logger.info(f"📋 第 {self._check_count} 次监控 ({datetime.now().strftime('%H:%M')})")
                    logger.info(f"{'='*60}")
                    
                    # 并行检查所有标的
                    tasks = [
                        self.check_symbol(sym, results)
                        for sym in self.config.get('symbols', [])
                    ]
                    await asyncio.gather(*tasks)
                    
                    # 发送定期报告（每次监控都发送）
                    await self.send_periodic_report(results)
                    
                else:
                    next_trading_time = datetime.now().replace(hour=9, minute=30, second=0)
                    if datetime.now() >= next_trading_time:
                        next_trading_time = next_trading_time.replace(day=next_trading_time.day + 1)
                    logger.info(f"⏸️  非交易时间，下次检查：{next_trading_time.strftime('%Y-%m-%d %H:%M')}")
                
                # 等待下一次检查
                await asyncio.sleep(self._check_interval)
            
            except Exception as e:
                logger.error(f"监控循环异常：{e}", exc_info=True)
                await asyncio.sleep(60)  # 异常后等待 1 分钟
    
    def stop(self):
        """停止监控"""
        self._running = False
        logger.info(f"🛑 监控已停止（共检查 {self._check_count} 次）")


async def main():
    """主函数"""
    config_path = Path(os.getenv('TRADEPILOT_CONFIG', 'config.yaml'))
    
    if not config_path.exists():
        logger.error("配置文件不存在：config.yaml")
        sys.exit(1)
    
    # 创建监控器
    monitor = MarketMonitor(str(config_path))
    
    # 处理停止信号
    def signal_handler(sig, frame):
        logger.info("收到停止信号...")
        monitor.stop()
    
    sys_signal.signal(sys_signal.SIGINT, signal_handler)
    sys_signal.signal(sys_signal.SIGTERM, signal_handler)
    
    # 启动监控
    try:
        await monitor.run_loop()
    except KeyboardInterrupt:
        logger.info("用户中断")
    finally:
        monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
