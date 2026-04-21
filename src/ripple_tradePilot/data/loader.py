from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from ripple_tradePilot.models.types import Bar


def load_csv(path: str | Path) -> Iterable[Bar]:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=False)
    for row in df.itertuples(index=False):
        yield Bar(
            timestamp=row.timestamp.to_pydatetime() if isinstance(row.timestamp, pd.Timestamp) else row.timestamp,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
