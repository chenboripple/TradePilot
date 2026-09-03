"""
纸面交易账本（跨会话持久化）

把 ``execution.executor.paper_trade`` 的运行记录落到与 ``user_store``
相同的 SQLite 数据库文件（``TRADEPILOT_BACKTEST_DB`` 或
``TRADEPILOT_DATA_DIR``，见 ``storage.database.database_path``），
新增 ``paper_runs`` / ``paper_fills`` 两张表：

- ``paper_runs``：每次纸面运行一行（策略、初始资金、期末权益）。
- ``paper_fills``：逐笔成交明细，按 ``run_id`` 关联。

``get_positions`` 汇总全部成交得到当前净持仓（买入量−卖出量），
供后续对账/仪表盘复用。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ripple_tradePilot.models.types import Fill, Side
from ripple_tradePilot.storage.database import database_path, init_database


def _target(path: Path | None) -> Path:
    target = init_database(path or database_path())
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_runs (
                run_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL DEFAULT '',
                strategy TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                initial_cash REAL NOT NULL,
                final_equity REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_fills_run "
            "ON paper_fills(run_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_fills_symbol "
            "ON paper_fills(symbol)"
        )
    return target


def _side_text(side: object) -> str:
    value = getattr(side, "value", side)
    return str(value).upper()


def record_run(
    run_id: str,
    symbol: str,
    strategy: str,
    initial_cash: float,
    final_equity: float,
    fills: Iterable[Fill],
    path: Path | None = None,
) -> str:
    """记录一次纸面运行及其成交明细，返回 run_id。"""
    target = _target(path)
    ordered = sorted(fills, key=lambda fill: fill.timestamp)
    created_at = datetime.now(timezone.utc).isoformat()
    started_at = ordered[0].timestamp.isoformat() if ordered else created_at
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO paper_runs (
                run_id, symbol, strategy, started_at, initial_cash, final_equity
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                symbol,
                strategy,
                started_at,
                float(initial_cash),
                float(final_equity),
            ),
        )
        connection.executemany(
            """
            INSERT INTO paper_fills (
                run_id, symbol, side, price, quantity, fee, timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    symbol,
                    _side_text(fill.side),
                    float(fill.price),
                    int(fill.quantity),
                    float(fill.fee),
                    fill.timestamp.isoformat(),
                    created_at,
                )
                for fill in ordered
            ],
        )
    return run_id


def list_runs(path: Path | None = None) -> List[Dict[str, Any]]:
    """全部纸面运行记录，按开始时间倒序。"""
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT run_id, symbol, strategy, started_at, initial_cash, final_equity
            FROM paper_runs
            ORDER BY started_at DESC, run_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_run_fills(run_id: str, path: Path | None = None) -> List[Dict[str, Any]]:
    """指定 run_id 的成交明细，按时间升序。"""
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, run_id, symbol, side, price, quantity, fee,
                   timestamp, created_at
            FROM paper_fills
            WHERE run_id = ?
            ORDER BY timestamp, id
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_positions(path: Path | None = None) -> List[Dict[str, Any]]:
    """按 symbol 汇总当前净持仓（买入量−卖出量），过滤已清零的标的。"""
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT symbol,
                   SUM(CASE WHEN side = ? THEN quantity ELSE 0 END)
                 - SUM(CASE WHEN side = ? THEN quantity ELSE 0 END) AS quantity
            FROM paper_fills
            GROUP BY symbol
            HAVING SUM(CASE WHEN side = ? THEN quantity ELSE 0 END)
                 - SUM(CASE WHEN side = ? THEN quantity ELSE 0 END) <> 0
            ORDER BY symbol
            """,
            (Side.BUY.value, Side.SELL.value, Side.BUY.value, Side.SELL.value),
        ).fetchall()
    return [{"symbol": row["symbol"], "quantity": row["quantity"]} for row in rows]
