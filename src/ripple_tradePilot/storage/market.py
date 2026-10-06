"""市场数据存取域：日线 / 指数 / 市场宽度 / 行业板块（自 database.py 拆出，P5）。

每个访问函数自带 ``init_database``（任何入口首次访问即补齐 schema）、
upsert 幂等、bool 落 INTEGER、时间戳走 CURRENT_TIMESTAMP——沿用房屋契约。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Mapping

from .schema import init_database

def load_daily_bars(
    symbol: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close, pre_close,
                   change, pct_chg, volume AS vol, amount, source,
                   adjust, adj_anchor_date, data_version
            FROM daily_bars
            WHERE symbol = ?
            ORDER BY trade_date
            """,
            (symbol,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_daily_bar_symbols(path: Path | None = None) -> List[str]:
    """列出 daily_bars 中出现过的全部标的（A9 全库复权巡检用）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        rows = connection.execute(
            "SELECT DISTINCT symbol FROM daily_bars "
            "WHERE symbol IS NOT NULL AND symbol != '' ORDER BY symbol"
        ).fetchall()
    return [row[0] for row in rows]


def upsert_daily_bars(
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
    adjust: str = "qfq",
    adj_anchor_date: str = "",
    data_version: str = "",
) -> int:
    """写入/更新某标的日线。

    A9 复权溯源：``adjust``/``adj_anchor_date``/``data_version`` 是序列级元数据
    （一次刷新一份），随整批写入；data_version 形如 ``source|anchor|utc_ts``，
    供 signal_ledger 与 ml manifest 引用做前视/混接溯源。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO daily_bars (
                symbol, trade_date, open, high, low, close,
                pre_close, change, pct_chg, volume, amount, source,
                adjust, adj_anchor_date, data_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                pre_close = excluded.pre_close,
                change = excluded.change,
                pct_chg = excluded.pct_chg,
                volume = excluded.volume,
                amount = excluded.amount,
                source = excluded.source,
                adjust = excluded.adjust,
                adj_anchor_date = excluded.adj_anchor_date,
                data_version = excluded.data_version,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    symbol,
                    row["trade_date"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row.get("pre_close"),
                    row.get("change"),
                    row.get("pct_chg"),
                    row.get("vol", 0),
                    row.get("amount"),
                    source,
                    adjust,
                    adj_anchor_date,
                    data_version,
                )
                for row in records
            ],
        )
    return len(records)


def upsert_index_daily(
    index_code: str,
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    """写入/更新某指数日线（C1）。

    ``rows`` 的 ``trade_date`` 须为 ``YYYYMMDD``（market_service 落库前统一归一化），
    成交量列接受 ``vol`` 或 ``volume``。按 ``(index_code, trade_date)`` upsert 幂等。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO index_daily (
                index_code, trade_date, open, high, low, close,
                pct_chg, amount, volume, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(index_code, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                pct_chg = excluded.pct_chg,
                amount = excluded.amount,
                volume = excluded.volume,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    index_code,
                    row.get("trade_date"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("pct_chg"),
                    row.get("amount"),
                    row.get("vol", row.get("volume")) or 0,
                    source,
                )
                for row in records
            ],
        )
    return len(records)


def load_index_bars(
    index_code: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """纯 DB 读取某指数日线（升序）。键与 ``load_daily_bars`` 对齐（成交量为 ``vol``）。

    离线只读，不触发任何网络。DB 优先 + 有界补拉的封装见
    ``data.market_service.load_index_bars``。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close, pct_chg,
                   amount, volume AS vol, source
            FROM index_daily
            WHERE index_code = ?
            ORDER BY trade_date
            """,
            (index_code,),
        ).fetchall()
    return [dict(row) for row in rows]


def record_market_daily(
    trade_date: str,
    breadth: Mapping[str, Any],
    source: str = "snapshot",
    path: Path | None = None,
) -> None:
    """写入/更新某交易日的市场宽度（C2）。

    ``breadth`` 取 ``aggregate_breadth`` 的规范输出（advancers/decliners/unchanged/
    limit_up/limit_down/total_amount/up_ratio）。按 ``trade_date`` 主键 upsert 幂等，
    同日重复刷新（盘中 provisional → 收盘 final）覆盖为最新终态。
    """
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO market_daily (
                trade_date, advancers, decliners, unchanged,
                limit_up, limit_down, total_amount, up_ratio, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(trade_date) DO UPDATE SET
                advancers = excluded.advancers,
                decliners = excluded.decliners,
                unchanged = excluded.unchanged,
                limit_up = excluded.limit_up,
                limit_down = excluded.limit_down,
                total_amount = excluded.total_amount,
                up_ratio = excluded.up_ratio,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                trade_date,
                int(breadth.get("advancers", 0) or 0),
                int(breadth.get("decliners", 0) or 0),
                int(breadth.get("unchanged", 0) or 0),
                int(breadth.get("limit_up", 0) or 0),
                int(breadth.get("limit_down", 0) or 0),
                float(breadth.get("total_amount", 0.0) or 0.0),
                breadth.get("up_ratio"),
                source,
            ),
        )


def load_market_daily(
    path: Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> List[Mapping[str, Any]]:
    """读取市场宽度历史（升序）。``start_date``/``end_date`` 为 ``YYYYMMDD`` 闭区间过滤。"""
    target = init_database(path)
    query = "SELECT * FROM market_daily"
    clauses: List[str] = []
    params: List[Any] = []
    if start_date:
        clauses.append("trade_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("trade_date <= ?")
        params.append(end_date)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY trade_date"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# v14（C3）：行业板块基建（东财 akshare 为唯一主源）
# ---------------------------------------------------------------------------
def upsert_industry_boards(
    records: Iterable[Mapping[str, Any]],
    source: str = "em",
    path: Path | None = None,
) -> int:
    """写入/更新行业板块登记表（C3）。``records`` 形如 ``{"board_code", "board_name"}``。

    按 ``board_code`` 主键 upsert 幂等。
    """
    rows = list(records)
    if not rows:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO industry_boards (board_code, board_name, source, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(board_code) DO UPDATE SET
                board_name = excluded.board_name,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    str(record.get("board_code")),
                    str(record.get("board_name") or ""),
                    source,
                )
                for record in rows
                if record.get("board_code")
            ],
        )
    return len(rows)


def load_industry_boards(path: Path | None = None) -> List[Mapping[str, Any]]:
    """读取板块登记表（按 board_code 升序）。离线只读，不触发网络。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT board_code, board_name, source FROM industry_boards ORDER BY board_code"
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_industry_board_bars(
    board_code: str,
    rows: Iterable[Mapping[str, Any]],
    source: str,
    path: Path | None = None,
) -> int:
    """写入/更新某板块日线（C3）。``rows`` 的 ``trade_date`` 须为 ``YYYYMMDD``
    （industry_service 落库前统一归一化），按 ``(board_code, trade_date)`` upsert 幂等。
    """
    records = list(rows)
    if not records:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO industry_board_bars (
                board_code, trade_date, open, high, low, close,
                pct_chg, amount, turnover_rate, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(board_code, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                pct_chg = excluded.pct_chg,
                amount = excluded.amount,
                turnover_rate = excluded.turnover_rate,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                (
                    board_code,
                    row.get("trade_date"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("pct_chg"),
                    row.get("amount"),
                    row.get("turnover_rate"),
                    source,
                )
                for row in records
            ],
        )
    return len(records)


def load_industry_board_bars(
    board_code: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """纯 DB 读取某板块日线（升序）。离线只读，不触发网络。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close, pct_chg,
                   amount, turnover_rate, source
            FROM industry_board_bars
            WHERE board_code = ?
            ORDER BY trade_date
            """,
            (board_code,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_industry_membership(
    board_code: str,
    symbols: Iterable[str],
    as_of: str,
    source: str = "em",
    path: Path | None = None,
) -> int:
    """写入/更新某板块成分股最新快照（C3）。

    按 ``(board_code, symbol)`` upsert，``as_of`` 记录观察日（``YYYYMMDD``）。现状单快照
    （非逐日 point-in-time）：成分变动靠下次刷新覆盖，离板旧行可能残留（D 阶段按
    as_of<=trade_date 取最新并标前视警告，诚实标注此局限）。
    """
    unique_symbols = sorted({str(symbol) for symbol in symbols if symbol})
    if not unique_symbols:
        return 0
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO industry_membership (board_code, symbol, as_of, source, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(board_code, symbol) DO UPDATE SET
                as_of = excluded.as_of,
                source = excluded.source,
                updated_at = CURRENT_TIMESTAMP
            """,
            [(board_code, symbol, as_of, source) for symbol in unique_symbols],
        )
    return len(unique_symbols)


def load_industry_membership(
    board_code: str | None = None, path: Path | None = None
) -> List[Mapping[str, Any]]:
    """读取成分股快照。给定 ``board_code`` 只返回该板块，否则全表（按 board_code, symbol）。"""
    target = init_database(path)
    query = "SELECT board_code, symbol, as_of, source FROM industry_membership"
    params: List[Any] = []
    if board_code:
        query += " WHERE board_code = ?"
        params.append(board_code)
    query += " ORDER BY board_code, symbol"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def industry_board_for_symbol(symbol: str, path: Path | None = None) -> str | None:
    """返回某股票所属板块代码（成分快照中 as_of 最新者；无则 None）。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        row = connection.execute(
            "SELECT board_code FROM industry_membership WHERE symbol = ? "
            "ORDER BY as_of DESC, board_code LIMIT 1",
            (symbol,),
        ).fetchone()
    return row[0] if row else None

