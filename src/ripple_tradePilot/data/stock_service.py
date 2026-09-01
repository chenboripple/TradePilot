from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import akshare as ak
import pandas as pd

from ripple_tradePilot.config_loader import load_config
from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.storage.database import (
    database_path,
    load_daily_bars,
    upsert_daily_bars,
)


_DATA_LOCK = threading.Lock()


class StockDataError(RuntimeError):
    pass


class InvalidStockSymbolError(StockDataError):
    pass


class StockDataUnavailableError(StockDataError):
    pass


class StockDataService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
        database: Optional[Path] = None,
    ):
        self.data_dir = data_dir or Path(
            os.getenv("TRADEPILOT_DATA_DIR", Path.cwd() / "data")
        )
        self.config_path = config_path or Path(
            os.getenv("TRADEPILOT_CONFIG", Path.cwd() / "config.yaml")
        )
        self.database = database or database_path()

    @staticmethod
    def normalize_symbol(value: str) -> str:
        raw = value.strip().upper()
        match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", raw)
        if not match:
            raise InvalidStockSymbolError("请输入 6 位股票代码，如 600309 或 000001.SZ")
        code, exchange = match.groups()
        if exchange is None:
            if code.startswith(("4", "8")):
                exchange = "BJ"
            elif code.startswith(("5", "6", "9")):
                exchange = "SH"
            else:
                exchange = "SZ"
        return f"{code}.{exchange}"

    def _configured_name(self, symbol: str) -> Optional[str]:
        config = load_config(str(self.config_path))
        for item in config.get("symbols", []):
            if str(item.get("code", "")).upper() == symbol:
                return item.get("name")
        return None

    @staticmethod
    def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy().rename(
            columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "vol",
                "成交额": "amount",
                "datetime": "trade_date",
                "timestamp": "trade_date",
                "volume": "vol",
            }
        )
        if "trade_date" not in data.columns:
            raise StockDataUnavailableError("行情数据缺少交易日期")
        for column in ("open", "high", "low", "close"):
            if column not in data.columns:
                raise StockDataUnavailableError(f"行情数据缺少 {column} 字段")
            data[column] = pd.to_numeric(data[column], errors="coerce")
        if "vol" not in data.columns:
            data["vol"] = 0
        data["vol"] = pd.to_numeric(data["vol"], errors="coerce").fillna(0)
        parsed_dates = pd.to_datetime(
            data["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True),
            errors="coerce",
        )
        numeric_dates = data["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True)
        numeric_mask = numeric_dates.str.fullmatch(r"\d{8}")
        parsed_dates.loc[numeric_mask] = pd.to_datetime(
            numeric_dates.loc[numeric_mask], format="%Y%m%d", errors="coerce"
        )
        data["trade_date"] = parsed_dates.dt.strftime("%Y%m%d")
        columns = ["trade_date", "open", "high", "low", "close", "vol"]
        if "amount" in data.columns:
            data["amount"] = pd.to_numeric(data["amount"], errors="coerce")
            columns.append("amount")
        return (
            data[columns]
            .dropna(subset=["trade_date", "open", "high", "low", "close"])
            .drop_duplicates(subset=["trade_date"], keep="last")
            .sort_values("trade_date")
            .reset_index(drop=True)
        )

    def _fetch_tushare(
        self, symbol: str, start_date: str, end_date: str
    ) -> Tuple[pd.DataFrame, Optional[str]]:
        config = load_config(str(self.config_path))
        tushare = config.get("tushare", {})
        token = tushare.get("token")
        if not token:
            return pd.DataFrame(), None
        loader = TushareDataLoader(
            token,
            rate_limit_delay=float(tushare.get("rate_limit_delay", 1.5)),
        )
        frame = loader.get_daily_bars(symbol, start_date=start_date, end_date=end_date)
        basic = None
        if len(frame):
            try:
                basic = loader.get_stock_basic(symbol)
            except Exception:
                basic = None
        return frame, basic.get("name") if basic else None

    @staticmethod
    def _fetch_akshare(
        symbol: str, start_date: str, end_date: str
    ) -> Tuple[pd.DataFrame, Optional[str]]:
        code = symbol.split(".", 1)[0]
        frame = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )
        name = None
        try:
            info = ak.stock_individual_info_em(symbol=code)
            if info is not None and {"item", "value"}.issubset(info.columns):
                rows = info[info["item"].astype(str).isin(("股票简称", "简称"))]
                if len(rows):
                    name = str(rows.iloc[0]["value"])
        except Exception:
            name = None
        return frame, name

    def refresh(self, value: str, initial_days: int = 365) -> Dict[str, Any]:
        symbol = self.normalize_symbol(value)
        now = datetime.now()
        with _DATA_LOCK:
            existing = pd.DataFrame(load_daily_bars(symbol, self.database))
            if len(existing):
                existing = self._normalize_frame(existing)
            if len(existing):
                latest = datetime.strptime(existing.iloc[-1]["trade_date"], "%Y%m%d")
                start = latest - timedelta(days=10)
            else:
                start = now - timedelta(days=initial_days)
            start_date = start.strftime("%Y%m%d")
            end_date = now.strftime("%Y%m%d")

            frame = pd.DataFrame()
            name = self._configured_name(symbol)
            source = "tushare"
            try:
                frame, fetched_name = self._fetch_tushare(symbol, start_date, end_date)
                name = fetched_name or name
            except Exception:
                frame = pd.DataFrame()
            if len(frame) == 0:
                source = "akshare"
                try:
                    frame, fetched_name = self._fetch_akshare(
                        symbol, start_date, end_date
                    )
                    name = fetched_name or name
                except Exception as error:
                    raise StockDataUnavailableError(
                        f"无法获取 {symbol} 的日线数据，请检查行情源配置"
                    ) from error
            fetched = self._normalize_frame(frame)
            if len(fetched) == 0:
                raise StockDataUnavailableError(f"未获取到 {symbol} 的有效日线数据")
            merged = self._normalize_frame(pd.concat([existing, fetched], ignore_index=True))
            upsert_daily_bars(
                symbol,
                fetched.to_dict(orient="records"),
                source,
                self.database,
            )

        return {
            "symbol": symbol,
            "name": name or symbol,
            "source": source,
            "fetched_rows": len(fetched),
            "total_rows": len(merged),
            "latest_date": datetime.strptime(
                merged.iloc[-1]["trade_date"], "%Y%m%d"
            ).date().isoformat(),
        }
