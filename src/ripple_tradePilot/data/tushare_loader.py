"""
Tushare 数据加载器
支持：日线数据、实时行情、股票列表

限流说明 (120 积分):
- 50 次/分钟
- 8000 次/天
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd
import tushare as ts
import akshare as ak

from ripple_tradePilot.models.types import Bar


class TushareDataLoader:
    """数据加载器：日线走 Tushare，当日分钟线/快照优先走 AkShare。"""
    
    def __init__(self, token: str, rate_limit_delay: float = 1.5):
        """
        初始化数据加载器
        
        Args:
            token: Tushare Token
            rate_limit_delay: 请求间隔秒数，默认 1.5 秒（确保不超过 50 次/分钟）
        """
        ts.set_token(token)
        self.pro = ts.pro_api()
        self._token = token
        self._rate_limit_delay = rate_limit_delay
        self._last_request_time = 0
    
    @staticmethod
    def _ts_to_ak_symbol(ts_code: str) -> str:
        """000999.SZ -> sz000999, 600309.SH -> sh600309"""
        code, exchange = ts_code.split('.')
        prefix = 'sz' if exchange.upper() == 'SZ' else 'sh'
        return f"{prefix}{code}"

    def _normalize_ak_minute_df(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or len(df) == 0:
            return pd.DataFrame()

        data = df.copy()
        rename_map = {
            '时间': 'datetime',
            '日期': 'datetime',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'vol',
            '成交额': 'amount',
        }
        data = data.rename(columns=rename_map)
        if 'datetime' not in data.columns:
            return pd.DataFrame()
        data['datetime'] = pd.to_datetime(data['datetime'])
        for col in ['open', 'high', 'low', 'close', 'vol']:
            if col in data.columns:
                data[col] = pd.to_numeric(data[col], errors='coerce')
        data = data.sort_values('datetime', ascending=True).reset_index(drop=True)
        return data[['datetime', 'open', 'high', 'low', 'close', 'vol']].dropna()

    def _get_ak_realtime_df(self) -> pd.DataFrame:
        df = ak.stock_zh_a_spot_em()
        if df is None or len(df) == 0:
            return pd.DataFrame()
        return df
    
    def _rate_limit(self):
        """限流保护：确保请求间隔不低于设定值"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        self._last_request_time = time.time()
    
    def get_stock_list(self) -> pd.DataFrame:
        """获取 A 股股票列表（限流：50 次/分钟）"""
        self._rate_limit()
        df = self.pro.stock_basic(
            exchange='',
            list_status='L',
            fields='ts_code,symbol,name,area,industry,market,list_status,list_date'
        )
        return df

    def get_stock_basic(self, ts_code: str) -> Optional[dict]:
        """获取单只股票的基础信息。"""
        self._rate_limit()
        df = self.pro.stock_basic(
            ts_code=ts_code,
            fields="ts_code,symbol,name,market,list_status,list_date",
        )
        if df is None or len(df) == 0:
            return None
        return df.iloc[0].to_dict()
    
    def get_daily_bars(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        """
        获取日线数据（限流：50 次/分钟）
        
        Args:
            ts_code: 股票代码，如 '002022.SZ'
            start_date: 开始日期，格式 'YYYYMMDD'，默认 30 天前
            end_date: 结束日期，格式 'YYYYMMDD'，默认今天
        
        Returns:
            DataFrame with columns: trade_date, open, high, low, close, vol, amount
        """
        self._rate_limit()
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365)).strftime('%Y%m%d')
        
        df = self.pro.daily(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date
        )
        
        if df is None or len(df) == 0:
            return pd.DataFrame()
        
        # 数据清洗
        df = df.sort_values('trade_date', ascending=True)
        df = df.reset_index(drop=True)
        
        return df
    
    def get_realtime_quote(self, ts_code: str) -> Optional[dict]:
        """获取实时行情快照，优先走 AkShare。"""
        try:
            code = ts_code.split('.')[0]
            df = self._get_ak_realtime_df()
            if len(df) == 0:
                return None

            code_col = '代码' if '代码' in df.columns else None
            if code_col is None:
                return None
            matched = df[df[code_col].astype(str) == str(code)]
            if len(matched) == 0:
                return None

            row = matched.iloc[0]
            return {
                'price': float(row.get('最新价', 0) or 0),
                'change': float(row.get('涨跌额', 0) or 0),
                'pct_change': float(row.get('涨跌幅', 0) or 0),
                'volume': float(row.get('成交量', 0) or 0) * 100,
                'amount': float(row.get('成交额', 0) or 0),
                'turnover': float(row.get('换手率', 0) or 0),
                'timestamp': datetime.now(),
            }
        except Exception as e:
            print(f"获取实时行情失败（AkShare）：{e}")
            return None

    def get_minute_bars(
        self,
        ts_code: str,
        start_dt: str | None = None,
        end_dt: str | None = None,
        freq: str = "1min",
    ) -> pd.DataFrame:
        """获取分钟线数据：优先 AkShare，当网络/代理异常时回退 Tushare。"""
        if end_dt is None:
            end_dt = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if start_dt is None:
            start_dt = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')

        try:
            ak_symbol = self._ts_to_ak_symbol(ts_code)
            period = freq.replace('min', '')
            if period not in {'1', '5', '15', '30', '60'}:
                period = '1'
            df = ak.stock_zh_a_hist_min_em(symbol=ak_symbol, period=period, adjust='')
            df = self._normalize_ak_minute_df(df)
            if len(df) > 0:
                start_ts = pd.to_datetime(start_dt)
                end_ts = pd.to_datetime(end_dt)
                df = df[(df['datetime'] >= start_ts) & (df['datetime'] <= end_ts)].reset_index(drop=True)
                if len(df) > 0:
                    return df
        except Exception as e:
            print(f"获取分钟线失败（AkShare）：{e}")

        # AkShare 不可用时，回退到 Tushare，避免监控链路断掉
        self._rate_limit()
        df = None
        try:
            df = self.pro.stk_mins(
                ts_code=ts_code,
                start_date=start_dt,
                end_date=end_dt,
                freq=freq,
            )
        except Exception:
            try:
                df = ts.pro_bar(
                    ts_code=ts_code,
                    start_date=start_dt,
                    end_date=end_dt,
                    freq=freq,
                )
            except Exception as e:
                print(f"获取分钟线失败（Tushare fallback）：{e}")
                return pd.DataFrame()

        if df is None or len(df) == 0:
            return pd.DataFrame()

        dt_col = 'trade_time' if 'trade_time' in df.columns else 'datetime' if 'datetime' in df.columns else None
        if dt_col is None:
            return pd.DataFrame()

        df = df.copy()
        df[dt_col] = pd.to_datetime(df[dt_col])
        df = df.sort_values(dt_col, ascending=True).reset_index(drop=True)
        df = df.rename(columns={dt_col: 'datetime'})
        return df
    
    def load_bars(
        self,
        ts_code: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> Iterable[Bar]:
        """
        加载日线为 Bar 迭代器（兼容回测引擎）
        """
        df = self.get_daily_bars(ts_code, start_date, end_date)
        
        for _, row in df.iterrows():
            try:
                trade_date = datetime.strptime(row['trade_date'], '%Y%m%d')
                yield Bar(
                    timestamp=trade_date,
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=float(row.get('vol', 0)) * 100,  # 手转股
                )
            except Exception as e:
                print(f"解析 K 线失败：{row}, 错误：{e}")
                continue

    def load_minute_bars(
        self,
        ts_code: str,
        start_dt: str | None = None,
        end_dt: str | None = None,
        freq: str = "1min",
    ) -> Iterable[Bar]:
        """加载分钟线为 Bar 迭代器（用于实时监控）。"""
        df = self.get_minute_bars(ts_code, start_dt, end_dt, freq=freq)

        for _, row in df.iterrows():
            try:
                yield Bar(
                    timestamp=row['datetime'].to_pydatetime() if hasattr(row['datetime'], 'to_pydatetime') else row['datetime'],
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=float(row.get('vol', 0)) * 100,
                )
            except Exception as e:
                print(f"解析分钟 K 线失败：{row}, 错误：{e}")
                continue

    def is_trade_day(self, date: datetime | None = None) -> bool:
        """判断当天是否为 A 股交易日。"""
        if date is None:
            date = datetime.now()

        # 先做快速过滤，避免周末误判
        if date.weekday() >= 5:
            return False

        try:
            self._rate_limit()
            cal = self.pro.trade_cal(
                exchange='SSE',
                start_date=date.strftime('%Y%m%d'),
                end_date=date.strftime('%Y%m%d'),
                fields='cal_date,is_open',
            )
            if cal is not None and len(cal) > 0:
                return str(cal.iloc[0].get('is_open', '0')) == '1'
        except Exception as e:
            print(f"获取交易日历失败，回退到工作日判断：{e}")

        return date.weekday() < 5
    
    def cache_to_csv(
        self,
        ts_code: str,
        output_path: str | Path,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """
        缓存数据到 CSV
        
        Returns:
            缓存的数据条数
        """
        df = self.get_daily_bars(ts_code, start_date, end_date)
        if len(df) == 0:
            return 0
        
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        
        return len(df)


# 使用示例
if __name__ == "__main__":
    import time
    
    TOKEN = "your_tushare_token_here"
    loader = TushareDataLoader(TOKEN)
    
    # 获取科华生物日线数据
    print("📈 获取科华生物 (002022.SZ) 日线数据...")
    df = loader.get_daily_bars('002022.SZ', start_date='20260101')
    print(f"   共 {len(df)} 条数据")
    print(f"\n最近 5 个交易日:")
    print(df[['trade_date', 'open', 'high', 'low', 'close', 'vol']].tail())
    
    # 缓存到 CSV
    print("\n💾 缓存到 data/002022.SZ.csv...")
    count = loader.cache_to_csv('002022.SZ', 'data/002022.SZ.csv', start_date='20260101')
    print(f"   缓存 {count} 条数据")
    
    # 测试 Bar 迭代器
    print("\n🔁 测试 Bar 迭代器...")
    bars = list(loader.load_bars('002022.SZ', start_date='20260101'))
    print(f"   共 {len(bars)} 个 Bar")
    if bars:
        print(f"   最新 Bar: 日期={bars[-1].timestamp}, 收盘价={bars[-1].close}")
    
    print("\n" + "="*50)
    print("✅ Tushare 数据加载器测试完成！")
    print("="*50)
    print("\n💡 提示：股票列表接口有限频，建议缓存使用")
