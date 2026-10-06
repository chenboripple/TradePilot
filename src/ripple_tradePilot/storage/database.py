"""SQLite 存储统一 facade（自本文件逐步拆分为按域子模块，P5）。

历史：本文件曾是 2700 行单模块。P5 起按域拆分为 schema / market / ml_store /
futures_store / catalog 子模块，本文件保留为**永久 facade**：所有历史导入路径
（``from ripple_tradePilot.storage.database import X`` / ``import database as db``）
继续生效，1079+ 存量测试零改动即为本拆分正确性的验收标准。

拆分进度：schema ✅ market/ml_store/futures_store/catalog 待拆。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


from .schema import (
    BACKTEST_COLUMNS,
    DAILY_BAR_COLUMNS,
    DATABASE_SCHEMA_VERSION,
    FUTURES_ACCOUNT_DAILY_COLUMNS,
    FUTURES_BAR_COLUMNS,
    FUTURES_CONTRACT_COLUMNS,
    FUTURES_ORDER_COLUMNS,
    FUTURES_QUOTE_COLUMNS,
    FUTURES_TRADE_COLUMNS,
    INDEX_DAILY_COLUMNS,
    INDUSTRY_BOARD_BARS_COLUMNS,
    INDUSTRY_BOARDS_COLUMNS,
    INDUSTRY_MEMBERSHIP_COLUMNS,
    KV_STORE_COLUMNS,
    MARKET_DAILY_COLUMNS,
    ML_DATASETS_COLUMNS,
    ML_MODELS_COLUMNS,
    NOTIFY_LOG_COLUMNS,
    SESSION_COLUMNS,
    SIGNAL_LEDGER_COLUMNS,
    STOCK_CATALOG_COLUMNS,
    STOCK_QUOTE_COLUMNS,
    STRATEGY_COLUMNS,
    USER_COLUMNS,
    WATCHLIST_COLUMNS,
    _INIT_DONE,
    _ensure_columns,
    check_database_integrity,
    database_path,
    init_database,
)
from .market import (
    industry_board_for_symbol,
    list_daily_bar_symbols,
    load_daily_bars,
    load_industry_board_bars,
    load_industry_boards,
    load_industry_membership,
    load_index_bars,
    load_market_daily,
    record_market_daily,
    upsert_daily_bars,
    upsert_index_daily,
    upsert_industry_board_bars,
    upsert_industry_boards,
    upsert_industry_membership,
)
from .ml_store import (
    _row_to_dataset_manifest,
    _row_to_model,
    list_datasets,
    list_models,
    load_dataset_manifest,
    load_model,
    load_promoted_model,
    register_dataset,
    register_model,
    retire_promoted_models,
    set_model_status,
)



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


# ── v16（期货 P1）：合约元数据 / K 线 / 报价快照 / 通知去重 ──────────────
# 全部沿用房屋契约：每个访问函数自带 init_database（任何入口首次访问即补齐 schema）、
# upsert 幂等、bool 落 INTEGER、时间戳走 CURRENT_TIMESTAMP。


def upsert_futures_contracts(
    rows: Iterable[Mapping[str, Any]], path: Path | None = None
) -> int:
    """写入/更新期货合约元数据（futures_meta 手维护表 + 交易所合约表合成）。

    按 ``symbol``（canonical）upsert；``expiry_is_approximate`` 接受 bool/int。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO futures_contracts (
                symbol, product, exchange, name, multiplier, tick_size,
                night_start, night_end, listed_date, expiry_date,
                expiry_is_approximate, rule_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                product = excluded.product,
                exchange = excluded.exchange,
                name = CASE WHEN excluded.name != '' THEN excluded.name
                            ELSE futures_contracts.name END,
                multiplier = excluded.multiplier,
                tick_size = excluded.tick_size,
                night_start = excluded.night_start,
                night_end = excluded.night_end,
                listed_date = CASE WHEN excluded.listed_date != ''
                                   THEN excluded.listed_date
                                   ELSE futures_contracts.listed_date END,
                expiry_date = CASE WHEN excluded.expiry_date != ''
                                   THEN excluded.expiry_date
                                   ELSE futures_contracts.expiry_date END,
                expiry_is_approximate = excluded.expiry_is_approximate,
                rule_version = excluded.rule_version,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    row.get("symbol"), row.get("product", ""),
                    row.get("exchange", ""), row.get("name", ""),
                    row.get("multiplier", 0), row.get("tick_size", 0),
                    row.get("night_start", ""), row.get("night_end", ""),
                    row.get("listed_date", ""), row.get("expiry_date", ""),
                    1 if row.get("expiry_is_approximate") else 0,
                    row.get("rule_version", ""),
                )
                for row in records
            ],
        )
    return len(records)


def load_futures_contracts(path: Path | None = None) -> List[Mapping[str, Any]]:
    """全部合约元数据（按 symbol 升序）。纯 DB 读，不触发网络。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT symbol, product, exchange, name, multiplier, tick_size,
                   night_start, night_end, listed_date, expiry_date,
                   expiry_is_approximate, rule_version, updated_at
            FROM futures_contracts ORDER BY symbol
            """
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_futures_bars(
    timeframe: str,
    rows: Iterable[Mapping[str, Any]],
    path: Path | None = None,
) -> int:
    """写入/更新期货 K 线（'1d' 日线 / '60m' 小时线）。

    行须含 ``symbol``（canonical）、``trade_date``（YYYYMMDD，夜盘已归属）、
    ``bar_time``（'60m' 结束时刻；日线空串）；按
    ``(symbol, timeframe, trade_date, bar_time)`` upsert 幂等（增量重拉不重复）。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO futures_bars (
                symbol, timeframe, trade_date, bar_time,
                open, high, low, close, volume, hold, settle, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, timeframe, trade_date, bar_time) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                hold = excluded.hold,
                settle = excluded.settle,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    row.get("symbol"), timeframe, row.get("trade_date", ""),
                    row.get("bar_time", ""),
                    row.get("open"), row.get("high"), row.get("low"),
                    row.get("close"), row.get("volume", 0) or 0,
                    row.get("hold", 0) or 0, row.get("settle"),
                    row.get("source", ""),
                )
                for row in records
            ],
        )
    return len(records)


def load_futures_bars(
    symbol: str,
    timeframe: str = "1d",
    limit: int | None = None,
    path: Path | None = None,
) -> List[Mapping[str, Any]]:
    """读取某合约 K 线（按 trade_date、bar_time 升序；limit 取**最新** N 根）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        sql = (
            "SELECT symbol, timeframe, trade_date, bar_time, open, high, low, close, "
            "volume, hold, settle, source, updated_at FROM futures_bars "
            "WHERE symbol = ? AND timeframe = ? ORDER BY trade_date, bar_time"
        )
        params: List[Any] = [symbol, timeframe]
        if limit is not None:
            # DESC 必须同时作用于两个排序键（只写尾部 DESC 只会倒序最后一列）
            sql = sql.replace(
                "ORDER BY trade_date, bar_time", "ORDER BY trade_date DESC, bar_time DESC"
            ) + " LIMIT ?"
            params.append(int(limit))
            rows = connection.execute(sql, params).fetchall()
            return [dict(row) for row in reversed(rows)]
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def latest_futures_bar(
    symbol: str, timeframe: str, path: Path | None = None
) -> Optional[Mapping[str, Any]]:
    """最新一根 K 线（新鲜度检查用）；无数据返回 None。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT symbol, timeframe, trade_date, bar_time, open, high, low, close, "
            "volume, hold, settle, source, updated_at FROM futures_bars "
            "WHERE symbol = ? AND timeframe = ? "
            "ORDER BY trade_date DESC, bar_time DESC LIMIT 1",
            (symbol, timeframe),
        ).fetchone()
    return dict(row) if row else None


def list_futures_bar_symbols(
    timeframe: str = "1d", path: Path | None = None
) -> List[str]:
    """futures_bars 中出现过日线的全部合约（主力判定取数范围用）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        rows = connection.execute(
            "SELECT DISTINCT symbol FROM futures_bars "
            "WHERE timeframe = ? AND symbol IS NOT NULL AND symbol != '' "
            "ORDER BY symbol",
            (timeframe,),
        ).fetchall()
    return [row[0] for row in rows]


def upsert_futures_quotes(
    rows: Iterable[Mapping[str, Any]], path: Path | None = None
) -> int:
    """写入/更新报价快照（futures_comm_info 口径，含每手保证金/涨跌停/主力标记）。"""
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO futures_quotes (
                symbol, price, upper_limit, lower_limit, margin_per_hand,
                margin_is_estimate, fee_per_lot, is_main, price_time,
                source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                price = excluded.price,
                upper_limit = excluded.upper_limit,
                lower_limit = excluded.lower_limit,
                margin_per_hand = excluded.margin_per_hand,
                margin_is_estimate = excluded.margin_is_estimate,
                fee_per_lot = excluded.fee_per_lot,
                is_main = excluded.is_main,
                price_time = excluded.price_time,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    row.get("symbol"), row.get("price"), row.get("upper_limit"),
                    row.get("lower_limit"), row.get("margin_per_hand"),
                    1 if row.get("margin_is_estimate") else 0,
                    row.get("fee_per_lot"), 1 if row.get("is_main") else 0,
                    row.get("price_time", ""), row.get("source", ""),
                )
                for row in records
            ],
        )
    return len(records)


def load_futures_quote(
    symbol: str, path: Path | None = None
) -> Optional[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT symbol, price, upper_limit, lower_limit, margin_per_hand, "
            "margin_is_estimate, fee_per_lot, is_main, price_time, source, updated_at "
            "FROM futures_quotes WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None


def load_futures_quotes(path: Path | None = None) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT symbol, price, upper_limit, lower_limit, margin_per_hand, "
            "margin_is_estimate, fee_per_lot, is_main, price_time, source, updated_at "
            "FROM futures_quotes ORDER BY symbol"
        ).fetchall()
    return [dict(row) for row in rows]


def record_notification(
    dedup_key: str,
    channel: str,
    kind: str,
    payload_json: str = "",
    path: Path | None = None,
) -> bool:
    """记录一次已发送通知；**去重键已存在返回 False（消费方据此跳过重发）**。

    roadmap P1：「对提醒设置持久化去重键，记录合约、周期、信号时间和策略版本，
    保证重启后不重复通知」——键的构成由调用方负责，本函数只保证幂等落库。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO notify_log (dedup_key, channel, kind, payload_json, sent_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (dedup_key, channel, kind, payload_json),
        )
        return cursor.rowcount == 1


def list_notifications(
    channel: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    path: Path | None = None,
) -> List[Mapping[str, Any]]:
    """最近通知记录（倒序），供复盘「连续 5 个交易日记录可追溯」验收。"""
    target = init_database(path)
    conditions: List[str] = []
    params: List[Any] = []
    if channel:
        conditions.append("channel = ?")
        params.append(channel)
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(int(limit))
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT dedup_key, channel, kind, payload_json, sent_at "
            f"FROM notify_log {where} ORDER BY sent_at DESC, dedup_key LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def delete_notification(dedup_key: str, path: Path | None = None) -> bool:
    """删除一条通知记录（重新武装该去重键）。

    用途：记录在先、发送在后——Webhook 投递失败时该提醒会被去重键永久压制；
    运维排障后删掉对应行，下一轮扫描即可重新通知。返回是否有行被删除。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        cursor = connection.execute(
            "DELETE FROM notify_log WHERE dedup_key = ?", (dedup_key,)
        )
        return cursor.rowcount == 1


def insert_futures_orders(
    rows: Iterable[Mapping[str, Any]], path: Path | None = None
) -> int:
    """追加写入期货回测订单生命周期记录（P2 审计底账，run_id 关联一次运行）。

    订单是不可变历史事件（含被拒/被撤），只追加不覆盖；重放报告按 run_id 读取。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO futures_orders (
                id, run_id, symbol, direction, open_close, lots, price, status,
                filled_lots, avg_fill_price, trade_date, bar_time, reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                (
                    row.get("id"), row.get("run_id", ""), row.get("symbol"),
                    row.get("direction"), row.get("open_close"), row.get("lots"),
                    row.get("price"), row.get("status"), row.get("filled_lots", 0),
                    row.get("avg_fill_price"), row.get("trade_date", ""),
                    row.get("bar_time", ""), row.get("reason", ""),
                )
                for row in records
            ],
        )
    return len(records)


def load_futures_orders(
    run_id: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """按 run_id 读取订单全生命周期（升序），报告重放与逐笔核对用。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, run_id, symbol, direction, open_close, lots, price, status, "
            "filled_lots, avg_fill_price, trade_date, bar_time, reason, "
            "created_at, updated_at "
            "FROM futures_orders WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def insert_futures_trades(
    rows: Iterable[Mapping[str, Any]], path: Path | None = None
) -> int:
    """追加写入期货回测成交明细（含费用与平仓盈亏、今昨拆分）。"""
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO futures_trades (
                run_id, order_id, symbol, direction, open_close, price, lots,
                fee, trade_date, bar_time, realized_pnl,
                close_from_yesterday, close_from_today, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                (
                    row.get("run_id", ""), row.get("order_id", ""),
                    row.get("symbol"), row.get("direction"),
                    row.get("open_close"), row.get("price"), row.get("lots"),
                    row.get("fee", 0), row.get("trade_date", ""),
                    row.get("bar_time", ""), row.get("realized_pnl"),
                    row.get("close_from_yesterday"), row.get("close_from_today"),
                )
                for row in records
            ],
        )
    return len(records)


def load_futures_trades(
    run_id: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """按 run_id 读取成交明细（升序），P2 验收「逐笔账目核对」的原始凭据。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, run_id, order_id, symbol, direction, open_close, price, "
            "lots, fee, trade_date, bar_time, realized_pnl, "
            "close_from_yesterday, close_from_today, created_at "
            "FROM futures_trades WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_futures_account_daily(
    rows: Iterable[Mapping[str, Any]], path: Path | None = None
) -> int:
    """写入/更新账户逐日快照（幂等：同 (run_id, trade_date) 覆盖最新口径）。"""
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO futures_account_daily (
                run_id, trade_date, balance, equity, available, margin_occupied,
                realized_pnl_today, position_pnl_today, fees_today,
                exposure_value, settlement_errors, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id, trade_date) DO UPDATE SET
                balance = excluded.balance,
                equity = excluded.equity,
                available = excluded.available,
                margin_occupied = excluded.margin_occupied,
                realized_pnl_today = excluded.realized_pnl_today,
                position_pnl_today = excluded.position_pnl_today,
                fees_today = excluded.fees_today,
                exposure_value = excluded.exposure_value,
                settlement_errors = excluded.settlement_errors,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    row.get("run_id", ""), row.get("trade_date", ""),
                    row.get("balance", 0), row.get("equity"),
                    row.get("available"), row.get("margin_occupied", 0),
                    row.get("realized_pnl_today", 0),
                    row.get("position_pnl_today", 0),
                    row.get("fees_today", 0), row.get("exposure_value"),
                    row.get("settlement_errors", ""),
                )
                for row in records
            ],
        )
    return len(records)


def load_futures_account_daily(
    run_id: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """按 run_id 读取账户逐日快照（按交易日升序），权益曲线与回撤计算源。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT run_id, trade_date, balance, equity, available, "
            "margin_occupied, realized_pnl_today, position_pnl_today, "
            "fees_today, exposure_value, settlement_errors, updated_at "
            "FROM futures_account_daily WHERE run_id = ? ORDER BY trade_date",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]
