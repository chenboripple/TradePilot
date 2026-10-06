"""标的目录与实时报价快照域（自 database.py 拆出，P5）。

stock_catalog（静态目录）/ stock_quotes（终态快照）存取；list_stock_catalog
联表 daily_bars + stock_quotes 组装标的列表（价格优先实时、回退日线）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional

from .schema import init_database

def stock_catalog_industries(path: Path | None = None) -> Mapping[str, str]:
    """轻量读取 ``stock_catalog`` 的 symbol→industry 静态标签（C3 兜底模糊匹配用）。

    东财成分快照拉取失败时，用此静态标签按板块名模糊匹配兜底；匹配不上则该股票行业
    特征缺失（绝不造假）。只返回 industry 非空的条目。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        rows = connection.execute(
            "SELECT symbol, industry FROM stock_catalog "
            "WHERE industry IS NOT NULL AND industry <> ''"
        ).fetchall()
    return {str(symbol): str(industry) for symbol, industry in rows}




def upsert_stock_catalog(
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO stock_catalog (
                symbol, name, market, exchange, board, industry, area,
                list_status, list_date, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                name = excluded.name,
                market = CASE
                    WHEN excluded.market <> '' THEN excluded.market
                    ELSE stock_catalog.market
                END,
                exchange = CASE
                    WHEN excluded.exchange <> '' THEN excluded.exchange
                    ELSE stock_catalog.exchange
                END,
                board = CASE
                    WHEN excluded.board <> '' THEN excluded.board
                    ELSE stock_catalog.board
                END,
                industry = CASE
                    WHEN excluded.industry <> '' THEN excluded.industry
                    ELSE stock_catalog.industry
                END,
                area = CASE
                    WHEN excluded.area <> '' THEN excluded.area
                    ELSE stock_catalog.area
                END,
                list_status = excluded.list_status,
                list_date = CASE
                    WHEN excluded.list_date <> '' THEN excluded.list_date
                    ELSE stock_catalog.list_date
                END,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    str(row["symbol"]).upper(),
                    str(row["name"]).strip(),
                    str(row.get("market") or row.get("board") or "").strip(),
                    str(row.get("exchange") or "").strip(),
                    str(row.get("board") or row.get("market") or "").strip(),
                    str(row.get("industry") or "").strip(),
                    str(row.get("area") or "").strip(),
                    str(row.get("list_status") or "L").strip(),
                    str(row.get("list_date") or "").strip(),
                    source,
                )
                for row in records
            ],
        )
        connection.execute(
            """
            UPDATE user_watchlist
            SET name = (
                SELECT stock_catalog.name
                FROM stock_catalog
                WHERE stock_catalog.symbol = user_watchlist.symbol
            )
            WHERE EXISTS (
                SELECT 1 FROM stock_catalog
                WHERE stock_catalog.symbol = user_watchlist.symbol
                  AND stock_catalog.name <> user_watchlist.name
            )
            """
        )
    return len(records)


def upsert_stock_quotes(
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO stock_quotes (
                symbol, price, pre_close, change, change_pct,
                open, high, low, volume, amount, turnover_rate,
                quote_time, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                price = excluded.price,
                pre_close = excluded.pre_close,
                change = excluded.change,
                change_pct = excluded.change_pct,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                volume = excluded.volume,
                amount = excluded.amount,
                turnover_rate = excluded.turnover_rate,
                quote_time = excluded.quote_time,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    str(row["symbol"]).upper(),
                    row["price"],
                    row.get("pre_close"),
                    row.get("change"),
                    row.get("change_pct"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("volume", 0),
                    row.get("amount"),
                    row.get("turnover_rate"),
                    str(row.get("quote_time") or ""),
                    source,
                )
                for row in records
            ],
        )
    return len(records)


def load_stock_quotes(path: Path | None = None) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT symbol, price, pre_close, change, change_pct,
                   open, high, low, volume, amount, turnover_rate,
                   quote_time, source
            FROM stock_quotes
            ORDER BY symbol
            """
        ).fetchall()
    return [dict(row) for row in rows]


def stock_catalog_name(symbol: str, path: Path | None = None) -> str | None:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        row = connection.execute(
            "SELECT name FROM stock_catalog WHERE symbol = ?", (symbol,)
        ).fetchone()
    return str(row[0]) if row else None


def stock_catalog_names(path: Path | None = None) -> Mapping[str, str]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        rows = connection.execute("SELECT symbol, name FROM stock_catalog").fetchall()
    return {str(symbol): str(name) for symbol, name in rows}


def list_stock_catalog(path: Path | None = None) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            WITH ranked_bars AS (
                SELECT
                    symbol,
                    trade_date,
                    close,
                    pre_close,
                    change,
                    pct_chg,
                    source,
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol ORDER BY trade_date DESC
                    ) AS position
                FROM daily_bars
            )
            SELECT
                stock_catalog.symbol,
                stock_catalog.name,
                stock_catalog.market,
                stock_catalog.exchange,
                stock_catalog.board,
                stock_catalog.industry,
                stock_catalog.area,
                stock_catalog.list_status,
                stock_catalog.list_date,
                stock_catalog.source,
                stock_catalog.updated_at,
                latest.trade_date AS latest_date,
                latest.close AS daily_price,
                latest.pre_close AS daily_pre_close,
                latest.change AS daily_change,
                latest.pct_chg AS daily_change_pct,
                latest.source AS daily_source,
                previous.close AS previous_price,
                stock_quotes.price AS quote_price,
                stock_quotes.pre_close AS quote_pre_close,
                stock_quotes.change AS quote_change,
                stock_quotes.change_pct AS quote_change_pct,
                stock_quotes.volume AS quote_volume,
                stock_quotes.amount AS quote_amount,
                stock_quotes.turnover_rate,
                stock_quotes.quote_time,
                stock_quotes.source AS quote_source
            FROM stock_catalog
            LEFT JOIN ranked_bars AS latest
                ON latest.symbol = stock_catalog.symbol AND latest.position = 1
            LEFT JOIN ranked_bars AS previous
                ON previous.symbol = stock_catalog.symbol AND previous.position = 2
            LEFT JOIN stock_quotes
                ON stock_quotes.symbol = stock_catalog.symbol
            ORDER BY stock_catalog.symbol
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        quote_price = item.pop("quote_price")
        daily_price = item.pop("daily_price")
        quote_pre_close = item.pop("quote_pre_close")
        daily_pre_close = item.pop("daily_pre_close")
        quote_change = item.pop("quote_change")
        daily_change = item.pop("daily_change")
        quote_change_pct = item.pop("quote_change_pct")
        daily_change_pct = item.pop("daily_change_pct")
        previous_price = item.pop("previous_price")
        using_quote = quote_price is not None
        price = quote_price if using_quote else daily_price
        pre_close = quote_pre_close if using_quote else daily_pre_close
        change = quote_change if using_quote else daily_change
        change_pct = quote_change_pct if using_quote else daily_change_pct
        comparison_price = pre_close if pre_close not in (None, 0) else previous_price
        if change is None and price is not None and comparison_price is not None:
            change = float(price) - float(comparison_price)
        if change_pct is None and price is not None and comparison_price not in (None, 0):
            change_pct = (float(price) / float(comparison_price) - 1) * 100
        item["price"] = price
        item["pre_close"] = pre_close
        item["change"] = change
        item["change_pct"] = change_pct
        item["price_time"] = (
            item.get("quote_time") if using_quote else item.get("latest_date")
        )
        item["price_source"] = (
            item.get("quote_source") if using_quote else item.get("daily_source")
        )
        item["price_kind"] = (
            "realtime"
            if using_quote
            else "daily" if price is not None else "unavailable"
        )
        if not using_quote:
            item["quote_volume"] = None
            item["quote_amount"] = None
            item["turnover_rate"] = None
            item["quote_time"] = None
        item.pop("quote_source", None)
        item.pop("daily_source", None)
        items.append(item)
    return items

