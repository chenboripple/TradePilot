from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ripple_tradePilot.storage.database import database_path, init_database


PASSWORD_ITERATIONS = 390_000
SESSION_DAYS = 14


class UserStoreError(RuntimeError):
    pass


class UsernameTakenError(UserStoreError):
    pass


class StrategyNotFoundError(UserStoreError):
    pass


class WatchlistExistsError(UserStoreError):
    pass


class WatchlistNotFoundError(UserStoreError):
    pass


def _target(path: Path | None) -> Path:
    return init_database(path or database_path())


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _public_user(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


def create_user(username: str, password: str, path: Path | None = None) -> Dict[str, Any]:
    target = _target(path)
    try:
        with sqlite3.connect(target, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            role = (
                "admin"
                if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                else "user"
            )
            cursor = connection.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, _password_hash(password), role),
            )
            row = connection.execute(
                "SELECT id, username, role, created_at FROM users WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as error:
        raise UsernameTakenError("用户名已被使用") from error
    return _public_user(row)


def authenticate_user(
    username: str, password: str, path: Path | None = None
) -> Optional[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, username, password_hash, role, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not _password_matches(password, row["password_hash"]):
        return None
    return _public_user(row)


def create_session(user_id: int, path: Path | None = None) -> str:
    target = _target(path)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            "DELETE FROM user_sessions WHERE expires_at <= ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
        connection.execute(
            "INSERT INTO user_sessions (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
            (user_id, token_hash, expires_at.isoformat()),
        )
    return token


def user_for_session(token: str, path: Path | None = None) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    target = _target(path)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT users.id, users.username, users.role, users.created_at
            FROM user_sessions
            JOIN users ON users.id = user_sessions.user_id
            WHERE user_sessions.token_hash = ? AND user_sessions.expires_at > ?
            """,
            (token_hash, datetime.now(timezone.utc).isoformat()),
        ).fetchone()
    return _public_user(row) if row else None


def delete_session(token: str, path: Path | None = None) -> None:
    if not token:
        return
    target = _target(path)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            "DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,)
        )


def create_strategy(
    user_id: int, strategy: Dict[str, Any], path: Path | None = None
) -> Dict[str, Any]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            INSERT INTO strategies (
                user_id, name, asset_class, symbol, profile,
                parameters_json, visibility
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                strategy["name"],
                strategy["asset_class"],
                strategy["symbol"],
                strategy["profile"],
                json.dumps(strategy["parameters"], ensure_ascii=False, separators=(",", ":")),
                strategy["visibility"],
            ),
        )
        row = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    return _strategy_dict(row, user_id)


def _strategy_dict(row: sqlite3.Row, viewer_id: int) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "asset_class": row["asset_class"],
        "symbol": row["symbol"],
        "profile": row["profile"],
        "parameters": json.loads(row["parameters_json"]),
        "visibility": row["visibility"],
        "owner": row["owner_username"],
        "is_owner": row["user_id"] == viewer_id,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_visible_strategies(
    viewer_id: int, path: Path | None = None
) -> List[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.user_id = ? OR strategies.visibility = 'public'
            ORDER BY strategies.user_id = ? DESC, strategies.updated_at DESC
            """,
            (viewer_id, viewer_id),
        ).fetchall()
    return [_strategy_dict(row, viewer_id) for row in rows]


def update_strategy_visibility(
    strategy_id: int,
    user_id: int,
    visibility: str,
    path: Path | None = None,
) -> Dict[str, Any]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            UPDATE strategies
            SET visibility = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ?
            """,
            (visibility, strategy_id, user_id),
        )
        if cursor.rowcount == 0:
            raise StrategyNotFoundError("策略不存在或无权修改")
        row = connection.execute(
            """
            SELECT strategies.*, users.username AS owner_username
            FROM strategies JOIN users ON users.id = strategies.user_id
            WHERE strategies.id = ?
            """,
            (strategy_id,),
        ).fetchone()
    return _strategy_dict(row, user_id)


def list_user_backtests(
    user_id: int, path: Path | None = None, limit: int = 100
) -> List[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT backtest_results.*, strategies.name AS strategy_name
            FROM backtest_results
            LEFT JOIN strategies ON strategies.id = backtest_results.strategy_id
            WHERE backtest_results.user_id = ?
            ORDER BY backtest_results.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _watchlist_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "name": row["name"],
        "created_at": row["created_at"],
        "last_updated_at": row["last_updated_at"],
        "user_added": True,
    }


def list_user_watchlist(
    user_id: int, path: Path | None = None
) -> List[Dict[str, Any]]:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, symbol, name, created_at, last_updated_at
            FROM user_watchlist
            WHERE user_id = ?
            ORDER BY created_at, id
            """,
            (user_id,),
        ).fetchall()
    return [_watchlist_dict(row) for row in rows]


def add_watchlist_item(
    user_id: int,
    symbol: str,
    name: str,
    path: Path | None = None,
) -> Dict[str, Any]:
    target = _target(path)
    try:
        with sqlite3.connect(target, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(
                """
                INSERT INTO user_watchlist (user_id, symbol, name, last_updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (user_id, symbol, name),
            )
            row = connection.execute(
                """
                SELECT id, symbol, name, created_at, last_updated_at
                FROM user_watchlist WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as error:
        raise WatchlistExistsError("该股票已在观察池中") from error
    return _watchlist_dict(row)


def watchlist_contains(
    user_id: int, symbol: str, path: Path | None = None
) -> bool:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        row = connection.execute(
            "SELECT 1 FROM user_watchlist WHERE user_id = ? AND symbol = ?",
            (user_id, symbol),
        ).fetchone()
    return row is not None


def mark_watchlist_updated(
    user_id: int, symbol: str, path: Path | None = None
) -> None:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            UPDATE user_watchlist SET last_updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND symbol = ?
            """,
            (user_id, symbol),
        )


def delete_watchlist_item(
    user_id: int, symbol: str, path: Path | None = None
) -> None:
    target = _target(path)
    with sqlite3.connect(target, timeout=30) as connection:
        cursor = connection.execute(
            "DELETE FROM user_watchlist WHERE user_id = ? AND symbol = ?",
            (user_id, symbol),
        )
        if cursor.rowcount == 0:
            raise WatchlistNotFoundError("观察池中不存在该股票")
