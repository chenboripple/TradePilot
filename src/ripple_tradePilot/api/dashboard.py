from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime
from math import sqrt
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ripple_tradePilot.config_loader import load_config


class DashboardDataError(RuntimeError):
    pass


class DashboardService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
        backtest_db: Optional[Path] = None,
        extra_symbols: Optional[Sequence[Dict[str, Any]]] = None,
        excluded_symbols: Optional[Sequence[str]] = None,
    ):
        self.data_dir = data_dir or Path(os.getenv("TRADEPILOT_DATA_DIR", Path.cwd() / "data"))
        self.config_path = config_path or Path(os.getenv("TRADEPILOT_CONFIG", Path.cwd() / "config.yaml"))
        self.backtest_db = backtest_db or self._resolve_backtest_db()
        self.extra_symbols = list(extra_symbols or [])
        self.excluded_symbols = {
            str(symbol).upper() for symbol in (excluded_symbols or [])
        }

    def _resolve_backtest_db(self) -> Path:
        configured = os.getenv("TRADEPILOT_BACKTEST_DB")
        if configured:
            return Path(configured)

        candidates = [
            self.data_dir / "backtest" / "backtest_results.db",
            Path.cwd() / "src" / "data" / "backtest" / "backtest_results.db",
        ]
        return next((path for path in candidates if path.exists()), candidates[0])

    def _config(self) -> Dict[str, Any]:
        return load_config(str(self.config_path))

    def _symbols(self) -> List[Dict[str, Any]]:
        config = self._config()
        assets = []
        for symbol in config.get("symbols", []):
            assets.append({**symbol, "asset_class": symbol.get("asset_class", "stock")})
        for future in config.get("futures", []):
            assets.append({**future, "asset_class": "future"})
        assets.extend(self.extra_symbols)
        unique: Dict[str, Dict[str, Any]] = {}
        for asset in assets:
            code = str(asset.get("code", "")).upper()
            if code and code not in self.excluded_symbols:
                unique[code] = {**unique.get(code, {}), **asset, "code": code}
        return list(unique.values())

    def configured_assets(self) -> List[Dict[str, Any]]:
        config = self._config()
        return [
            {
                **symbol,
                "code": str(symbol.get("code", "")).upper(),
                "asset_class": symbol.get("asset_class", "stock"),
            }
            for symbol in config.get("symbols", [])
            if symbol.get("code")
        ]

    def configured_symbols(self) -> List[str]:
        config = self._config()
        return [
            str(symbol.get("code", "")).upper()
            for symbol in config.get("symbols", [])
            if symbol.get("code")
        ]

    def _profile(self, symbol: Dict[str, Any]) -> Dict[str, Any]:
        config = self._config()
        profiles = {
            **config.get("strategy_profiles", {}),
            **config.get("futures_strategy_profiles", {}),
        }
        return profiles.get(symbol.get("strategy_profile", ""), {})

    def _read_bars(self, symbol: str) -> List[Dict[str, Any]]:
        database_bars = self._read_database_bars(symbol)
        if database_bars:
            return database_bars

        path = self.data_dir / f"{symbol}.csv"
        if not path.exists():
            raise DashboardDataError(f"行情文件不存在: {path.name}")

        bars: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                raw_date = str(row.get("trade_date") or row.get("timestamp") or "")
                try:
                    timestamp = datetime.strptime(raw_date[:8], "%Y%m%d")
                except ValueError:
                    try:
                        timestamp = datetime.fromisoformat(raw_date)
                    except ValueError:
                        continue

                try:
                    bars.append(
                        {
                            "timestamp": timestamp,
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row.get("vol") or row.get("volume") or 0),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue

        bars.sort(key=lambda item: item["timestamp"])
        if not bars:
            raise DashboardDataError(f"行情文件无有效数据: {path.name}")
        return bars

    def _read_database_bars(self, symbol: str) -> List[Dict[str, Any]]:
        if not self.backtest_db.exists():
            return []
        try:
            with sqlite3.connect(self.backtest_db) as connection:
                rows = connection.execute(
                    """
                    SELECT trade_date, open, high, low, close, volume
                    FROM daily_bars
                    WHERE symbol = ?
                    ORDER BY trade_date
                    """,
                    (symbol,),
                ).fetchall()
        except sqlite3.Error:
            return []
        bars = []
        for trade_date, open_price, high, low, close, volume in rows:
            try:
                timestamp = datetime.strptime(str(trade_date)[:10], "%Y-%m-%d")
            except ValueError:
                try:
                    timestamp = datetime.strptime(str(trade_date)[:8], "%Y%m%d")
                except ValueError:
                    continue
            bars.append(
                {
                    "timestamp": timestamp,
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume or 0),
                }
            )
        return bars

    @staticmethod
    def _rolling_mean(values: List[float], window: int) -> List[Optional[float]]:
        result: List[Optional[float]] = [None] * len(values)
        running_total = 0.0
        for index, value in enumerate(values):
            running_total += value
            if index >= window:
                running_total -= values[index - window]
            if index >= window - 1:
                result[index] = running_total / window
        return result

    @staticmethod
    def _rsi(values: List[float], period: int) -> List[Optional[float]]:
        result: List[Optional[float]] = [None] * len(values)
        for index in range(period, len(values)):
            changes = [values[position] - values[position - 1] for position in range(index - period + 1, index + 1)]
            gains = sum(max(change, 0) for change in changes) / period
            losses = sum(max(-change, 0) for change in changes) / period
            result[index] = 100.0 if losses == 0 else 100 - (100 / (1 + gains / losses))
        return result

    @staticmethod
    def _bollinger(values: List[float], period: int, multiplier: float) -> Dict[str, List[Optional[float]]]:
        upper: List[Optional[float]] = [None] * len(values)
        middle: List[Optional[float]] = [None] * len(values)
        lower: List[Optional[float]] = [None] * len(values)
        for index in range(period - 1, len(values)):
            window = values[index - period + 1:index + 1]
            average = sum(window) / period
            deviation = sqrt(sum((value - average) ** 2 for value in window) / period)
            middle[index] = average
            upper[index] = average + multiplier * deviation
            lower[index] = average - multiplier * deviation
        return {"upper": upper, "middle": middle, "lower": lower}

    @staticmethod
    def _profile_parameters(profile: Dict[str, Any]) -> Dict[str, Any]:
        ma_config = profile.get("ma", {})
        rsi_config = profile.get("rsi", {})
        bb_config = profile.get("bb", {})
        return {
            "ma_fast": int(profile.get("ma_fast", ma_config.get("fast", 5))),
            "ma_slow": int(profile.get("ma_slow", ma_config.get("slow", 20))),
            "rsi_period": int(profile.get("rsi_period", rsi_config.get("period", 14))),
            "rsi_oversold": float(profile.get("rsi_oversold", rsi_config.get("oversold", 30))),
            "rsi_overbought": float(profile.get("rsi_overbought", rsi_config.get("overbought", 70))),
            "bb_period": int(profile.get("bb_period", bb_config.get("period", 20))),
            "bb_std": float(profile.get("bb_std", bb_config.get("std_dev", 2.0))),
            "vote_threshold": int(profile.get("vote_threshold", 2)),
        }

    @staticmethod
    def _decision(
        close: float,
        fast_ma: Optional[float],
        slow_ma: Optional[float],
        rsi: Optional[float],
        upper: Optional[float],
        lower: Optional[float],
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        votes = {"ma": "HOLD", "rsi": "HOLD", "bollinger": "HOLD"}
        if fast_ma is not None and slow_ma is not None:
            votes["ma"] = "BUY" if fast_ma > slow_ma else "SELL"
        if rsi is not None:
            if rsi <= parameters["rsi_oversold"]:
                votes["rsi"] = "BUY"
            elif rsi >= parameters["rsi_overbought"]:
                votes["rsi"] = "SELL"
        if upper is not None and lower is not None:
            if close <= lower:
                votes["bollinger"] = "BUY"
            elif close >= upper:
                votes["bollinger"] = "SELL"

        buy_count = sum(value == "BUY" for value in votes.values())
        sell_count = sum(value == "SELL" for value in votes.values())
        threshold = parameters["vote_threshold"]
        if buy_count >= threshold and buy_count > sell_count:
            recommendation = "BUY"
        elif sell_count >= threshold and sell_count > buy_count:
            recommendation = "SELL"
        else:
            recommendation = "HOLD"

        reasons = []
        if votes["ma"] != "HOLD":
            reasons.append(f"短期均线{'高于' if votes['ma'] == 'BUY' else '低于'}长期均线")
        if votes["rsi"] != "HOLD" and rsi is not None:
            reasons.append(f"RSI {rsi:.1f} 进入{'超卖' if votes['rsi'] == 'BUY' else '超买'}区")
        if votes["bollinger"] != "HOLD":
            reasons.append(f"价格触及布林带{'下轨' if votes['bollinger'] == 'BUY' else '上轨'}")
        if not reasons:
            reasons.append("指标未形成一致方向")

        return {
            "recommendation": recommendation,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "confidence": round(max(buy_count, sell_count) / len(votes) * 100),
            "votes": votes,
            "reason": "；".join(reasons),
        }

    def market_detail(self, symbol_code: str, limit: int = 160) -> Dict[str, Any]:
        symbol = next((item for item in self._symbols() if item["code"] == symbol_code), None)
        if symbol is None:
            raise DashboardDataError(f"未配置标的: {symbol_code}")

        bars = self._read_bars(symbol_code)
        profile = self._profile(symbol)
        parameters = self._profile_parameters(profile)
        closes = [bar["close"] for bar in bars]
        fast_ma = self._rolling_mean(closes, parameters["ma_fast"])
        slow_ma = self._rolling_mean(closes, parameters["ma_slow"])
        rsi_values = self._rsi(closes, parameters["rsi_period"])
        bands = self._bollinger(closes, parameters["bb_period"], parameters["bb_std"])

        decisions: List[Dict[str, Any]] = []
        signals: List[Dict[str, Any]] = []
        previous_recommendation = "HOLD"
        for index, bar in enumerate(bars):
            decision = self._decision(
                bar["close"],
                fast_ma[index],
                slow_ma[index],
                rsi_values[index],
                bands["upper"][index],
                bands["lower"][index],
                parameters,
            )
            decisions.append(decision)
            if decision["recommendation"] in {"BUY", "SELL"} and decision["recommendation"] != previous_recommendation:
                signals.append(
                    {
                        "date": bar["timestamp"].date().isoformat(),
                        "side": decision["recommendation"],
                        "price": bar["close"],
                        "reason": decision["reason"],
                    }
                )
            previous_recommendation = decision["recommendation"]

        latest = bars[-1]
        previous = bars[-2] if len(bars) > 1 else latest
        latest_decision = decisions[-1]
        lag_days = max((datetime.now().date() - latest["timestamp"].date()).days, 0)
        start = max(len(bars) - max(40, min(limit, 260)), 0)

        chart_bars = []
        for index in range(start, len(bars)):
            bar = bars[index]
            chart_bars.append(
                {
                    "date": bar["timestamp"].date().isoformat(),
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ma_fast": fast_ma[index],
                    "ma_slow": slow_ma[index],
                    "bb_upper": bands["upper"][index],
                    "bb_middle": bands["middle"][index],
                    "bb_lower": bands["lower"][index],
                }
            )

        return {
            "symbol": symbol_code,
            "name": symbol.get("name", symbol_code),
            "asset_class": symbol.get("asset_class", "stock"),
            "exchange": symbol.get("exchange", symbol_code.rsplit(".", 1)[-1] if "." in symbol_code else ""),
            "strategy_profile": symbol.get("strategy_profile", "未配置"),
            "profile_kind": profile.get("kind", "unknown"),
            "parameters": parameters,
            "price": latest["close"],
            "change": latest["close"] - previous["close"],
            "change_pct": (latest["close"] / previous["close"] - 1) * 100 if previous["close"] else 0,
            "latest_date": latest["timestamp"].date().isoformat(),
            "freshness": "fresh" if lag_days <= 4 else "stale",
            "lag_days": lag_days,
            "recommendation": latest_decision["recommendation"],
            "confidence": latest_decision["confidence"],
            "buy_count": latest_decision["buy_count"],
            "sell_count": latest_decision["sell_count"],
            "votes": latest_decision["votes"],
            "reason": latest_decision["reason"],
            "indicators": {
                "ma_fast": fast_ma[-1],
                "ma_slow": slow_ma[-1],
                "rsi": rsi_values[-1],
                "bb_upper": bands["upper"][-1],
                "bb_middle": bands["middle"][-1],
                "bb_lower": bands["lower"][-1],
            },
            "bars": chart_bars,
            "signals": signals[-8:][::-1],
            "total_rows": len(bars),
            "user_added": bool(symbol.get("user_added")),
        }

    def strategy_catalog(self) -> List[Dict[str, Any]]:
        strategies = []
        for symbol in self._symbols():
            try:
                item = self.market_detail(symbol["code"])
            except DashboardDataError:
                continue
            strategies.append(
                {
                    "id": f"system:{item['symbol']}",
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "asset_class": item["asset_class"],
                    "profile": item["strategy_profile"],
                    "kind": item["profile_kind"],
                    "parameters": item["parameters"],
                    "recommendation": item["recommendation"],
                    "confidence": item["confidence"],
                    "visibility": "public",
                    "owner": "TradePilot",
                    "is_owner": False,
                    "is_system": True,
                }
            )
        return strategies

    def dashboard(self) -> Dict[str, Any]:
        details = []
        errors = []
        for symbol in self._symbols():
            try:
                details.append(self.market_detail(symbol["code"]))
            except DashboardDataError as error:
                errors.append(
                    {
                        "symbol": symbol["code"],
                        "asset_class": symbol.get("asset_class", "stock"),
                        "error": str(error),
                    }
                )

        recommendation_counts = {
            side: sum(item["recommendation"] == side for item in details)
            for side in ("BUY", "SELL", "HOLD")
        }
        latest_date = max((item["latest_date"] for item in details), default=None)
        asset_counts = {
            asset_class: {
                "configured": sum(item.get("asset_class") == asset_class for item in self._symbols()),
                "available": sum(item["asset_class"] == asset_class for item in details),
                "buy": sum(item["asset_class"] == asset_class and item["recommendation"] == "BUY" for item in details),
                "sell": sum(item["asset_class"] == asset_class and item["recommendation"] == "SELL" for item in details),
                "hold": sum(item["asset_class"] == asset_class and item["recommendation"] == "HOLD" for item in details),
            }
            for asset_class in ("stock", "future")
        }
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "summary": {
                "symbols": len(details),
                "buy": recommendation_counts["BUY"],
                "sell": recommendation_counts["SELL"],
                "hold": recommendation_counts["HOLD"],
                "stale": sum(item["freshness"] == "stale" for item in details),
                "latest_date": latest_date,
                "by_asset": asset_counts,
            },
            "markets": details,
            "system": {
                "api": "online",
                "data_source": "SQLite daily bars + CSV legacy fallback",
                "database": "SQLite unified storage",
                "config_path": str(self.config_path),
                "data_dir": str(self.data_dir),
                "errors": errors,
            },
        }
