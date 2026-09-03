from __future__ import annotations

import itertools
from pathlib import Path
from typing import Iterable, Iterator

import pandas as pd

from ripple_tradePilot.models.types import Bar

# 日期列候选，按优先级尝试（真实行情 CSV 常用 trade_date）
DATE_COLUMNS = ("trade_date", "timestamp", "datetime", "date")
# 成交量列候选（真实行情 CSV 常用 vol），都缺失时成交量记 0
VOLUME_COLUMNS = ("volume", "vol")


def _parse_dates(series: pd.Series) -> pd.Series:
    """把日期列解析为 datetime，兼容 20250421 这类整数日期。"""
    if pd.api.types.is_integer_dtype(series):
        # 整数日期必须先转字符串，否则会被当作纳秒级 epoch 时间戳
        series = series.astype(str)
    try:
        return pd.to_datetime(series, format="%Y%m%d")
    except (ValueError, TypeError):
        return pd.to_datetime(series)


def load_csv(path: str | Path) -> Iterable[Bar]:
    """从 CSV 读取 OHLCV 数据，返回按时间升序的 ``Bar`` 迭代器。

    日期列按 ``trade_date`` / ``timestamp`` / ``datetime`` / ``date``
    的优先级尝试；成交量列兼容 ``volume`` / ``vol``，缺失时记 0。
    """
    path = Path(path)
    df = pd.read_csv(path)

    date_col = next((c for c in DATE_COLUMNS if c in df.columns), None)
    if date_col is None:
        raise ValueError(
            f"CSV {path.name} 缺少日期列；现有列 {list(df.columns)}，"
            f"需要其中之一：{', '.join(DATE_COLUMNS)}"
        )

    missing = {"open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"CSV {path.name} 缺少必需列：{', '.join(sorted(missing))}")

    timestamps = _parse_dates(df[date_col])

    vol_col = next((c for c in VOLUME_COLUMNS if c in df.columns), None)
    volumes: Iterator[float] = (float(v) for v in df[vol_col]) if vol_col else itertools.repeat(0.0)

    for ts, o, h, l, c, v in zip(timestamps, df["open"], df["high"], df["low"], df["close"], volumes):
        yield Bar(
            timestamp=ts.to_pydatetime(),
            open=float(o),
            high=float(h),
            low=float(l),
            close=float(c),
            volume=v,
        )
