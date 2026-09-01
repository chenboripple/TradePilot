from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Mapping


BACKTEST_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT",
    "name": "name TEXT",
    "start_date": "start_date TEXT",
    "end_date": "end_date TEXT",
    "initial_capital": "initial_capital REAL",
    "final_capital": "final_capital REAL",
    "total_return": "total_return REAL",
    "annual_return": "annual_return REAL",
    "max_drawdown": "max_drawdown REAL",
    "sharpe_ratio": "sharpe_ratio REAL",
    "total_trades": "total_trades INTEGER",
    "win_rate": "win_rate REAL",
    "created_at": "created_at TIMESTAMP",
    "user_id": "user_id INTEGER",
    "strategy_id": "strategy_id INTEGER",
}

USER_COLUMNS = {
    "id": "id INTEGER",
    "username": "username TEXT",
    "password_hash": "password_hash TEXT",
    "role": "role TEXT DEFAULT 'user'",
    "created_at": "created_at TIMESTAMP",
}

SESSION_COLUMNS = {
    "id": "id INTEGER",
    "user_id": "user_id INTEGER",
    "token_hash": "token_hash TEXT",
    "expires_at": "expires_at TIMESTAMP",
    "created_at": "created_at TIMESTAMP",
}

STRATEGY_COLUMNS = {
    "id": "id INTEGER",
    "user_id": "user_id INTEGER",
    "name": "name TEXT",
    "asset_class": "asset_class TEXT DEFAULT 'stock'",
    "symbol": "symbol TEXT DEFAULT ''",
    "profile": "profile TEXT DEFAULT ''",
    "parameters_json": "parameters_json TEXT DEFAULT '{}'",
    "visibility": "visibility TEXT DEFAULT 'private'",
    "created_at": "created_at TIMESTAMP",
    "updated_at": "updated_at TIMESTAMP",
}

WATCHLIST_COLUMNS = {
    "id": "id INTEGER",
    "user_id": "user_id INTEGER",
    "symbol": "symbol TEXT DEFAULT ''",
    "name": "name TEXT DEFAULT ''",
    "is_watched": "is_watched INTEGER NOT NULL DEFAULT 1",
    "created_at": "created_at TIMESTAMP",
    "last_updated_at": "last_updated_at TIMESTAMP",
}

DAILY_BAR_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT DEFAULT ''",
    "trade_date": "trade_date TEXT DEFAULT ''",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "volume": "volume REAL DEFAULT 0",
    "amount": "amount REAL",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

STOCK_CATALOG_COLUMNS = {
    "symbol": "symbol TEXT DEFAULT ''",
    "name": "name TEXT DEFAULT ''",
    "market": "market TEXT DEFAULT ''",
    "list_status": "list_status TEXT DEFAULT 'L'",
    "list_date": "list_date TEXT DEFAULT ''",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}


def database_path() -> Path:
    configured = os.getenv("TRADEPILOT_BACKTEST_DB")
    if configured:
        return Path(configured)

    data_dir = Path(os.getenv("TRADEPILOT_DATA_DIR", Path.cwd() / "data"))
    return data_dir / "backtest" / "backtest_results.db"


def _ensure_columns(connection: sqlite3.Connection, table_name: str, columns: Mapping[str, str]) -> None:
    existing = {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }
    for column_name, definition in columns.items():
        if column_name not in existing:
            connection.execute(f'ALTER TABLE "{table_name}" ADD COLUMN {definition}')


def init_database(path: Path | None = None) -> Path:
    target = path or database_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            """
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
            """
        )
        _ensure_columns(connection, "backtest_results", BACKTEST_COLUMNS)
        connection.execute(
            "UPDATE backtest_results SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_results_symbol_created "
            "ON backtest_results(symbol, created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_results_user_created "
            "ON backtest_results(user_id, created_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'admin')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "users", USER_COLUMNS)
        connection.execute(
            "UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        connection.execute("UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''")
        has_admin = connection.execute(
            "SELECT 1 FROM users WHERE role = 'admin' LIMIT 1"
        ).fetchone()
        first_user = connection.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        if not has_admin and first_user:
            connection.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?", (first_user[0],)
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
            "ON users(username COLLATE NOCASE)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_columns(connection, "user_sessions", SESSION_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_user "
            "ON user_sessions(user_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_expiry "
            "ON user_sessions(expires_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                asset_class TEXT NOT NULL CHECK(asset_class IN ('stock', 'future')),
                symbol TEXT NOT NULL,
                profile TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'private'
                    CHECK(visibility IN ('public', 'private')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_columns(connection, "strategies", STRATEGY_COLUMNS)
        connection.execute(
            "UPDATE strategies SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
        )
        connection.execute(
            "UPDATE strategies SET updated_at = created_at WHERE updated_at IS NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategies_owner_updated "
            "ON strategies(user_id, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategies_visibility_updated "
            "ON strategies(visibility, updated_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                name TEXT NOT NULL,
                is_watched INTEGER NOT NULL DEFAULT 1 CHECK(is_watched IN (0, 1)),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_updated_at TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, symbol)
            )
            """
        )
        _ensure_columns(connection, "user_watchlist", WATCHLIST_COLUMNS)
        connection.execute(
            "UPDATE user_watchlist SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL"
        )
        connection.execute(
            "UPDATE user_watchlist SET is_watched = 1 WHERE is_watched IS NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_watchlist_owner_symbol "
            "ON user_watchlist(user_id, symbol)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_watchlist_owner_created "
            "ON user_watchlist(user_id, created_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_catalog (
                symbol TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT NOT NULL DEFAULT '',
                list_status TEXT NOT NULL DEFAULT 'L',
                list_date TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "stock_catalog", STOCK_CATALOG_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_stock_catalog_name "
            "ON stock_catalog(name)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL DEFAULT 0,
                amount REAL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trade_date)
            )
            """
        )
        _ensure_columns(connection, "daily_bars", DAILY_BAR_COLUMNS)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_bars_symbol_date "
            "ON daily_bars(symbol, trade_date)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_bars_date "
            "ON daily_bars(trade_date DESC)"
        )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {target}: {integrity}")
        connection.execute("PRAGMA user_version=7")

    return target


def insert_backtest_result(
    result: Any,
    path: Path | None = None,
    user_id: int | None = None,
    strategy_id: int | None = None,
) -> Path:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO backtest_results (
                symbol, name, start_date, end_date,
                initial_capital, final_capital, total_return, annual_return,
                max_drawdown, sharpe_ratio, total_trades, win_rate,
                user_id, strategy_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.symbol,
                result.name,
                result.start_date,
                result.end_date,
                result.initial_capital,
                result.final_capital,
                result.total_return,
                result.annual_return,
                result.max_drawdown,
                result.sharpe_ratio,
                result.total_trades,
                result.win_rate,
                user_id,
                strategy_id,
            ),
        )
    return target


def load_daily_bars(
    symbol: str, path: Path | None = None
) -> List[Mapping[str, Any]]:
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close,
                   volume AS vol, amount, source
            FROM daily_bars
            WHERE symbol = ?
            ORDER BY trade_date
            """,
            (symbol,),
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_daily_bars(
    symbol: str,
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
            INSERT INTO daily_bars (
                symbol, trade_date, open, high, low, close,
                volume, amount, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                amount = excluded.amount,
                source = excluded.source,
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
                    row.get("vol", 0),
                    row.get("amount"),
                    source,
                )
                for row in records
            ],
        )
    return len(records)


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
                symbol, name, market, list_status, list_date, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                name = excluded.name,
                market = CASE
                    WHEN excluded.market <> '' THEN excluded.market
                    ELSE stock_catalog.market
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
                    str(row.get("market") or "").strip(),
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
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol ORDER BY trade_date DESC
                    ) AS position
                FROM daily_bars
            )
            SELECT
                stock_catalog.symbol,
                stock_catalog.name,
                stock_catalog.market,
                stock_catalog.list_status,
                stock_catalog.list_date,
                stock_catalog.source,
                stock_catalog.updated_at,
                latest.trade_date AS latest_date,
                latest.close AS price,
                previous.close AS previous_price
            FROM stock_catalog
            LEFT JOIN ranked_bars AS latest
                ON latest.symbol = stock_catalog.symbol AND latest.position = 1
            LEFT JOIN ranked_bars AS previous
                ON previous.symbol = stock_catalog.symbol AND previous.position = 2
            ORDER BY stock_catalog.symbol
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        price = item.pop("price")
        previous_price = item.pop("previous_price")
        item["price"] = price
        item["change"] = (
            float(price) - float(previous_price)
            if price is not None and previous_price is not None
            else None
        )
        item["change_pct"] = (
            (float(price) / float(previous_price) - 1) * 100
            if price is not None and previous_price not in (None, 0)
            else None
        )
        items.append(item)
    return items
