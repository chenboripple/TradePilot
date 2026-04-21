"""
TradePilot 回测引擎

功能：
- 获取历史数据（Tushare）
- 运行策略回测
- 生成绩效报告
- 发送飞书通知
- 保存回测结果
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.models.types import Bar, Side
from ripple_tradePilot.notifiers.feishu import FeishuWebhookNotifier


class BacktestResult:
    """回测结果"""
    
    def __init__(self):
        self.symbol: str = ""
        self.name: str = ""
        self.start_date: str = ""
        self.end_date: str = ""
        self.initial_capital: float = 100000.0
        self.final_capital: float = 100000.0
        self.total_return: float = 0.0
        self.annual_return: float = 0.0
        self.max_drawdown: float = 0.0
        self.sharpe_ratio: float = 0.0
        self.win_rate: float = 0.0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'name': self.name,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'initial_capital': self.initial_capital,
            'final_capital': self.final_capital,
            'total_return': self.total_return,
            'annual_return': self.annual_return,
            'max_drawdown': self.max_drawdown,
            'sharpe_ratio': self.sharpe_ratio,
            'win_rate': self.win_rate,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'trades': self.trades,
            'equity_curve': self.equity_curve,
        }
    
    def to_json(self, indent: int = 2) -> str:
        """转换为 JSON"""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


class BacktestEngine:
    """回测引擎"""
    
    def __init__(
        self,
        token: str,
        initial_capital: float = 100000.0,
        commission: float = 0.0003,  # 万三手续费
        slippage: float = 0.001,  # 千一滑点
    ):
        self.token = token
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        
        self.data_loader = TushareDataLoader(token)
        
        # 策略（使用更敏感的参数以产生更多信号）
        self.ma_strategy = MovingAverageCross(fast=3, slow=10)
        self.rsi_strategy = RSI(period=10, oversold=35, overbought=65)
        self.bb_strategy = BollingerBands(period=14, std_dev=1.8)
    
    def get_data(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
    ) -> List[Bar]:
        """获取历史数据"""
        bars = list(self.data_loader.load_bars(ts_code, start_date, end_date))
        return bars
    
    def generate_signal(self, bar: Bar, history: List[Bar]) -> Optional[Side]:
        """生成交易信号（三策略投票）"""
        if len(history) < 20:
            return None
        
        # 重置策略
        self.ma_strategy.reset()
        self.rsi_strategy.reset()
        self.bb_strategy.reset()
        
        # 预热
        for prev_bar in history[:-1]:
            self.ma_strategy.on_bar(prev_bar)
            self.rsi_strategy.on_bar(prev_bar)
            self.bb_strategy.on_bar(prev_bar)
        
        # 生成信号
        ma_signal = self.ma_strategy.on_bar(bar)
        rsi_signal = self.rsi_strategy.on_bar(bar)
        bb_signal = self.bb_strategy.on_bar(bar)
        
        # 投票
        buy_count = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.BUY)
        sell_count = sum(1 for s in [ma_signal, rsi_signal, bb_signal] if s.side == Side.SELL)
        
        if buy_count >= 2:
            return Side.BUY
        elif sell_count >= 2:
            return Side.SELL
        else:
            return None
    
    def run(self, ts_code: str, name: str, start_date: str, end_date: str) -> BacktestResult:
        """运行回测"""
        print(f"\n🔍 开始回测：{name} ({ts_code})")
        print(f"   区间：{start_date} 至 {end_date}")
        
        # 获取数据
        bars = self.get_data(ts_code, start_date, end_date)
        if not bars:
            raise ValueError(f"无法获取 {ts_code} 的数据")
        
        print(f"   数据条数：{len(bars)}")
        
        # 初始化
        capital = self.initial_capital
        position = 0  # 持仓数量
        entry_price = 0  # 入场价格
        
        result = BacktestResult()
        result.symbol = ts_code
        result.name = name
        result.start_date = start_date
        result.end_date = end_date
        result.initial_capital = self.initial_capital
        
        trades = []
        equity_curve = []
        peak_capital = capital
        
        # 回测主循环
        for i, bar in enumerate(bars):
            history = bars[:i+1]
            signal = self.generate_signal(bar, history)
            
            # 交易逻辑
            if signal == Side.BUY and position == 0:
                # 买入
                buy_price = bar.close * (1 + self.slippage)
                shares = int(capital * 0.95 / buy_price / 100) * 100  # 95% 仓位，100 股整数倍
                if shares > 0:
                    cost = shares * buy_price * (1 + self.commission)
                    if cost <= capital:
                        capital -= cost
                        position = shares
                        entry_price = buy_price
            
            elif signal == Side.SELL and position > 0:
                # 卖出
                sell_price = bar.close * (1 - self.slippage)
                revenue = position * sell_price * (1 - self.commission)
                capital += revenue
                
                # 记录交易
                pnl = (sell_price - entry_price) * position * (1 - self.commission * 2)
                pnl_pct = (sell_price / entry_price - 1) * 100
                
                trades.append({
                    'date': bar.timestamp.strftime('%Y-%m-%d'),
                    'type': 'SELL',
                    'price': sell_price,
                    'shares': position,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                })
                
                position = 0
                entry_price = 0
        
        # 计算最终资产
        if position > 0:
            # 按最新价计算持仓价值
            final_value = capital + position * bars[-1].close
        else:
            final_value = capital
        
        result.final_capital = final_value
        result.total_return = (final_value - self.initial_capital) / self.initial_capital * 100
        
        # 计算年化收益
        days = (bars[-1].timestamp - bars[0].timestamp).days
        if days > 0:
            result.annual_return = ((final_value / self.initial_capital) ** (365 / days) - 1) * 100
        
        # 计算交易统计
        result.total_trades = len(trades)
        result.winning_trades = sum(1 for t in trades if t['pnl'] > 0)
        result.losing_trades = sum(1 for t in trades if t['pnl'] <= 0)
        
        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades * 100
        
        # 计算最大回撤和夏普比率（简化版）
        equity_values = [self.initial_capital]
        current_capital = self.initial_capital
        
        for bar, trade_signal in zip(bars, [self.generate_signal(b, bars[:j+1]) for j, b in enumerate(bars)]):
            if position > 0:
                current_capital = capital + position * bar.close
            equity_values.append(current_capital)
            peak_capital = max(peak_capital, current_capital)
        
        drawdown = (peak_capital - min(equity_values)) / peak_capital * 100 if peak_capital > 0 else 0
        result.max_drawdown = drawdown
        
        # 简化夏普比率计算
        if len(equity_values) > 1:
            returns = [(equity_values[i] - equity_values[i-1]) / equity_values[i-1] 
                      for i in range(1, len(equity_values))]
            import numpy as np
            if np.std(returns) > 0:
                result.sharpe_ratio = np.mean(returns) / np.std(returns) * np.sqrt(252)
        
        result.trades = trades
        result.equity_curve = [{'date': b.timestamp.strftime('%Y-%m-%d'), 'equity': e} 
                               for b, e in zip(bars, equity_values[:len(bars)])]
        
        # 打印结果
        print(f"\n📊 回测结果:")
        print(f"   初始资金：¥{self.initial_capital:,.2f}")
        print(f"   最终资金：¥{final_value:,.2f}")
        print(f"   总收益率：{result.total_return:.2f}%")
        print(f"   年化收益：{result.annual_return:.2f}%")
        print(f"   最大回撤：{result.max_drawdown:.2f}%")
        print(f"   夏普比率：{result.sharpe_ratio:.2f}")
        print(f"   交易次数：{result.total_trades}")
        print(f"   胜率：{result.win_rate:.2f}%")
        
        return result


class BacktestManager:
    """回测管理器"""
    
    def __init__(self, config_path: str = "config.yaml"):
        import yaml
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        self.token = self.config['tushare']['token']
        self.engine = BacktestEngine(self.token)
        
        # 飞书通知
        feishu_config = self.config.get('notifiers', {}).get('feishu', {})
        if feishu_config.get('enabled', False):
            self.feishu = FeishuWebhookNotifier(
                webhook_url=feishu_config.get('webhook', ''),
                secret=feishu_config.get('secret')
            )
        else:
            self.feishu = None
        
        # 数据存储
        self.data_dir = Path(__file__).parent.parent.parent / "data" / "backtest"
        self.data_dir.mkdir(parents=True, exist_ok=True)
    
    def run_backtest(
        self,
        ts_code: str,
        name: str,
        days: int = 90,
        save: bool = True,
        send_feishu: bool = True,
    ) -> BacktestResult:
        """
        运行回测
        
        Args:
            ts_code: 股票代码
            name: 股票名称
            days: 回测天数（默认 90 天，约 3 个月）
            save: 是否保存结果
            send_feishu: 是否发送飞书通知
        """
        # 计算日期范围
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        start_str = start_date.strftime('%Y%m%d')
        end_str = end_date.strftime('%Y%m%d')
        
        # 运行回测
        result = self.engine.run(ts_code, name, start_str, end_str)
        
        # 保存结果
        if save:
            self._save_result(result)
        
        # 发送飞书通知
        if send_feishu and self.feishu:
            self._send_feishu_report(result)
        
        return result
    
    def _save_result(self, result: BacktestResult):
        """保存回测结果"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # 保存 JSON
        json_path = self.data_dir / f"{result.symbol}_{timestamp}_result.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            f.write(result.to_json())
        print(f"\n💾 结果已保存：{json_path}")
        
        # 保存交易记录 CSV
        if result.trades:
            csv_path = self.data_dir / f"{result.symbol}_{timestamp}_trades.csv"
            df = pd.DataFrame(result.trades)
            df.to_csv(csv_path, index=False)
            print(f"💾 交易记录已保存：{csv_path}")
        
        # 保存到 SQLite
        db_path = self.data_dir / "backtest_results.db"
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 创建表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS backtest_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                name TEXT,
                start_date TEXT,
                end_date TEXT,
                initial_capital REAL,
                final_capital REAL,
                total_return REAL,
                annual_return REAL,
                max_drawdown REAL,
                sharpe_ratio REAL,
                total_trades INTEGER,
                win_rate REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 插入数据
        cursor.execute('''
            INSERT INTO backtest_results (
                symbol, name, start_date, end_date,
                initial_capital, final_capital, total_return, annual_return,
                max_drawdown, sharpe_ratio, total_trades, win_rate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            result.symbol, result.name, result.start_date, result.end_date,
            result.initial_capital, result.final_capital, result.total_return,
            result.annual_return, result.max_drawdown, result.sharpe_ratio,
            result.total_trades, result.win_rate
        ))
        
        conn.commit()
        conn.close()
        print(f"💾 数据库已更新：{db_path}")
    
    def _send_feishu_report(self, result: BacktestResult):
        """发送飞书回测报告"""
        # 构建消息
        color = "red" if result.total_return > 0 else "green" if result.total_return < 0 else "blue"
        emoji = "🟢" if result.total_return > 0 else "🔴" if result.total_return < 0 else "⚪"
        
        content = {
            "msg_type": "text",
            "content": {
                "text": f"{emoji} TradePilot 回测报告\n\n"
                        f"标的：{result.name} ({result.symbol})\n"
                        f"区间：{result.start_date} 至 {result.end_date}\n\n"
                        f"📊 收益情况:\n"
                        f"• 初始资金：¥{result.initial_capital:,.2f}\n"
                        f"• 最终资金：¥{result.final_capital:,.2f}\n"
                        f"• 总收益率：{result.total_return:.2f}%\n"
                        f"• 年化收益：{result.annual_return:.2f}%\n\n"
                        f"📉 风险指标:\n"
                        f"• 最大回撤：{result.max_drawdown:.2f}%\n"
                        f"• 夏普比率：{result.sharpe_ratio:.2f}\n\n"
                        f"💹 交易统计:\n"
                        f"• 交易次数：{result.total_trades}\n"
                        f"• 胜率：{result.win_rate:.2f}%\n"
                        f"• 盈利：{result.winning_trades} | 亏损：{result.losing_trades}\n\n"
                        f"⚠️ 历史回测不代表未来表现，投资需谨慎"
            }
        }
        
        try:
            self.feishu._send_text(content)
            print("✅ 飞书报告已发送")
        except Exception as e:
            print(f"❌ 发送飞书报告失败：{e}")


# 命令行入口
if __name__ == "__main__":
    import sys
    
    # 默认回测 002022 科华生物
    ts_code = sys.argv[1] if len(sys.argv) > 1 else "002022.SZ"
    name = sys.argv[2] if len(sys.argv) > 2 else "科华生物"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 90
    
    manager = BacktestManager()
    manager.run_backtest(ts_code, name, days)
