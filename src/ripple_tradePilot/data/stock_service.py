from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import akshare as ak
import httpx
import pandas as pd

from ripple_tradePilot.config_loader import load_config
from ripple_tradePilot.data.tushare_loader import TushareDataLoader
from ripple_tradePilot.storage.database import (
    database_path,
    load_daily_bars,
    stock_catalog_name,
    upsert_daily_bars,
    upsert_stock_catalog,
    upsert_stock_quotes,
)


_DATA_LOCK = threading.Lock()
_CATALOG_LOCK = threading.Lock()
_QUOTE_LOCK = threading.Lock()


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
            if code.startswith(("4", "8", "92")):
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
    def _valid_name(value: Any, symbol: str) -> Optional[str]:
        name = str(value or "").strip()
        if not name or name.lower() in {"nan", symbol.lower(), symbol.split(".", 1)[0]}:
            return None
        return name

    @staticmethod
    def _catalog_records(frame: pd.DataFrame) -> list[Dict[str, str]]:
        if not len(frame) or not {"ts_code", "name"}.issubset(frame.columns):
            return []
        records = []
        for _, row in frame.iterrows():
            symbol = str(row.get("ts_code") or "").upper().strip()
            name = StockDataService._valid_name(row.get("name"), symbol)
            if not symbol or not name:
                continue
            derived = StockDataService._market_metadata(symbol)
            board = str(row.get("market") or derived["board"]).strip()
            records.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": board,
                    "exchange": str(
                        row.get("exchange") or derived["exchange"]
                    ).strip(),
                    "board": board,
                    "industry": str(row.get("industry") or "").strip(),
                    "area": str(row.get("area") or "").strip(),
                    "list_status": str(row.get("list_status") or "L").strip(),
                    "list_date": str(row.get("list_date") or "").strip(),
                }
            )
        return records

    @staticmethod
    def _market_metadata(symbol: str) -> Dict[str, str]:
        code, suffix = symbol.upper().split(".", 1)
        if suffix == "BJ":
            return {"exchange": "BSE", "board": "北交所"}
        if suffix == "SH":
            board = "科创板" if code.startswith(("688", "689")) else "主板"
            return {"exchange": "SSE", "board": board}
        board = "创业板" if code.startswith(("300", "301")) else "主板"
        return {"exchange": "SZSE", "board": board}

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(str(value).replace(",", "").replace("%", ""))
        except (TypeError, ValueError):
            return None
        return None if pd.isna(number) else number

    @staticmethod
    def _realtime_records(frame: pd.DataFrame) -> list[Dict[str, Any]]:
        if frame is None or len(frame) == 0 or "代码" not in frame.columns:
            return []
        quote_time = datetime.now().isoformat(timespec="seconds")
        records = []
        for _, row in frame.iterrows():
            code = str(row.get("代码") or "").strip().zfill(6)
            if not re.fullmatch(r"\d{6}", code):
                continue
            try:
                symbol = StockDataService.normalize_symbol(code)
            except InvalidStockSymbolError:
                continue
            price = StockDataService._optional_float(row.get("最新价"))
            if price is None:
                continue
            volume = StockDataService._optional_float(row.get("成交量"))
            records.append(
                {
                    "symbol": symbol,
                    "price": price,
                    "pre_close": StockDataService._optional_float(row.get("昨收")),
                    "change": StockDataService._optional_float(row.get("涨跌额")),
                    "change_pct": StockDataService._optional_float(row.get("涨跌幅")),
                    "open": StockDataService._optional_float(row.get("今开")),
                    "high": StockDataService._optional_float(row.get("最高")),
                    "low": StockDataService._optional_float(row.get("最低")),
                    "volume": volume * 100 if volume is not None else 0,
                    "amount": StockDataService._optional_float(row.get("成交额")),
                    "turnover_rate": StockDataService._optional_float(
                        row.get("换手率")
                    ),
                    "quote_time": quote_time,
                }
            )
        return records

    @staticmethod
    def _eastmoney_catalog_records() -> list[Dict[str, str]]:
        params = {
            "pn": 1,
            "pz": 10000,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f14",
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        endpoints = (
            "https://82.push2.eastmoney.com/api/qt/clist/get",
            "https://push2.eastmoney.com/api/qt/clist/get",
            "https://88.push2.eastmoney.com/api/qt/clist/get",
        )
        last_error: Optional[Exception] = None
        rows = []
        for endpoint in endpoints:
            try:
                response = httpx.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=30,
                    follow_redirects=True,
                )
                response.raise_for_status()
                rows = (response.json().get("data") or {}).get("diff") or []
                if rows:
                    break
                last_error = StockDataUnavailableError("东方财富返回了空股票清单")
            except Exception as error:
                last_error = error
        if not rows:
            raise StockDataUnavailableError("东方财富股票清单请求失败") from last_error
        records = []
        for row in rows:
            code = str(row.get("f12") or "").strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            symbol = StockDataService.normalize_symbol(code)
            name = StockDataService._valid_name(row.get("f14"), symbol)
            if not name:
                continue
            metadata = StockDataService._market_metadata(symbol)
            records.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": metadata["board"],
                    **metadata,
                    "industry": "",
                    "area": "",
                    "list_status": "L",
                    "list_date": "",
                }
            )
        return records

    @staticmethod
    def _sina_catalog_records() -> list[Dict[str, str]]:
        count_url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeStockCount"
        )
        data_url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData"
        )
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://finance.sina.com.cn/stock/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        rows = []
        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
            count_response = client.get(count_url, params={"node": "hs_a"})
            count_response.raise_for_status()
            total = int(count_response.json())
            for page in range(1, (total + 99) // 100 + 1):
                response = client.get(
                    data_url,
                    params={
                        "page": page,
                        "num": 100,
                        "sort": "symbol",
                        "asc": 1,
                        "node": "hs_a",
                    },
                )
                response.raise_for_status()
                page_rows = response.json()
                if not isinstance(page_rows, list):
                    raise StockDataUnavailableError("新浪返回了无效的股票清单")
                rows.extend(page_rows)

        records = []
        exchange_names = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
        for row in rows:
            code = str(row.get("code") or "").strip()
            raw_symbol = str(row.get("symbol") or "").lower().strip()
            exchange = exchange_names.get(raw_symbol[:2])
            if not re.fullmatch(r"\d{6}", code) or not exchange:
                continue
            symbol = f"{code}.{exchange}"
            name = StockDataService._valid_name(row.get("name"), symbol)
            if not name:
                continue
            metadata = StockDataService._market_metadata(symbol)
            records.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "market": metadata["board"],
                    **metadata,
                    "industry": "",
                    "area": "",
                    "list_status": "L",
                    "list_date": "",
                }
            )
        return records

    def refresh_catalog(self) -> Dict[str, Any]:
        config = load_config(str(self.config_path))
        tushare = config.get("tushare", {})
        token = tushare.get("token")
        with _CATALOG_LOCK:
            records = []
            source = ""
            if token:
                try:
                    loader = TushareDataLoader(
                        token,
                        rate_limit_delay=float(tushare.get("rate_limit_delay", 1.5)),
                    )
                    records = self._catalog_records(loader.get_stock_list())
                    source = "tushare"
                except Exception:
                    records = []
            if not records:
                try:
                    records = self._eastmoney_catalog_records()
                    source = "eastmoney"
                except Exception:
                    try:
                        records = self._sina_catalog_records()
                        source = "sina"
                    except Exception as error:
                        raise StockDataUnavailableError(
                            "暂时无法获取股票清单，请稍后重试"
                        ) from error
            if not records:
                raise StockDataUnavailableError("未获取到有效的股票清单")
            upsert_stock_catalog(records, source, self.database)
        return {"count": len(records), "source": source}

    def refresh_quotes(self) -> Dict[str, Any]:
        with _QUOTE_LOCK:
            try:
                frame = ak.stock_zh_a_spot_em()
            except Exception as error:
                raise StockDataUnavailableError(
                    "暂时无法获取全市场实时行情，请稍后重试"
                ) from error
            records = self._realtime_records(frame)
            if not records:
                raise StockDataUnavailableError("全市场实时行情返回了空数据")
            upsert_stock_quotes(records, "akshare", self.database)
        return {
            "count": len(records),
            "source": "akshare",
            "quote_time": records[0]["quote_time"],
        }

    @staticmethod
    def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy().rename(
            columns={
                "日期": "trade_date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "昨收": "pre_close",
                "涨跌额": "change",
                "涨跌幅": "pct_chg",
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
        for column in ("pre_close", "change", "pct_chg"):
            if column in data.columns:
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
        columns.extend(
            column
            for column in ("pre_close", "change", "pct_chg")
            if column in data.columns
        )
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
    ) -> pd.DataFrame:
        config = load_config(str(self.config_path))
        tushare = config.get("tushare", {})
        token = tushare.get("token")
        if not token:
            return pd.DataFrame()
        loader = TushareDataLoader(
            token,
            rate_limit_delay=float(tushare.get("rate_limit_delay", 1.5)),
        )
        return loader.get_daily_bars(
            symbol, start_date=start_date, end_date=end_date
        )

    @staticmethod
    def _fetch_akshare(
        symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        code = symbol.split(".", 1)[0]
        frame = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )
        return frame

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
            name = stock_catalog_name(symbol, self.database) or self._configured_name(
                symbol
            )
            source = "tushare"
            try:
                frame = self._fetch_tushare(symbol, start_date, end_date)
            except Exception:
                frame = pd.DataFrame()
            if len(frame) == 0:
                source = "akshare"
                try:
                    frame = self._fetch_akshare(symbol, start_date, end_date)
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
