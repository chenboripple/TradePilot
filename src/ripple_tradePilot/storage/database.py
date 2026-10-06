from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DATABASE_SCHEMA_VERSION = 17

# 进程内已完成 schema 引导的库路径（init-once 缓存，见 init_database 文档）
_INIT_DONE: set[Path] = set()


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
    # 记录回测入参，供前端按原参数一键重跑
    "strategy_key": "strategy_key TEXT",
    "bar_count": "bar_count INTEGER",
    "execution": "execution TEXT",
    # 完整回测结果（权益曲线/成交明细/指标），供前端历史回放
    "result_json": "result_json TEXT",
    # v12（A8）：CLI 回测/walk-forward 落库与来源溯源。
    # run_kind='backtest'|'walkforward'；user_id 为 NULL 表示 CLI 跑的非用户记录
    # （list_user_backtests 按 user_id 过滤，NULL 行不进用户列表）。
    "run_kind": "run_kind TEXT DEFAULT 'backtest'",
    "params_json": "params_json TEXT",
    "profile_source": "profile_source TEXT",
    "report_json": "report_json TEXT",
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
    "system_key": "system_key TEXT",
}

WATCHLIST_COLUMNS = {
    "id": "id INTEGER",
    "user_id": "user_id INTEGER",
    "symbol": "symbol TEXT DEFAULT ''",
    "name": "name TEXT DEFAULT ''",
    "is_watched": "is_watched INTEGER NOT NULL DEFAULT 1",
    "created_at": "created_at TIMESTAMP",
    "last_updated_at": "last_updated_at TIMESTAMP",
    "default_strategy_id": "default_strategy_id INTEGER",
}

DAILY_BAR_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT DEFAULT ''",
    "trade_date": "trade_date TEXT DEFAULT ''",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "pre_close": "pre_close REAL",
    "change": "change REAL",
    "pct_chg": "pct_chg REAL",
    "volume": "volume REAL DEFAULT 0",
    "amount": "amount REAL",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
    # v12（A9）：复权溯源。adjust='qfq'|'raw'；adj_anchor_date=复权锚定日
    # （拉取时最新交易日，混接根因）；data_version='source|anchor|utc_ts'
    # （signal_ledger 与 ml manifest 引用做溯源）。
    "adjust": "adjust TEXT DEFAULT 'qfq'",
    "adj_anchor_date": "adj_anchor_date TEXT DEFAULT ''",
    "data_version": "data_version TEXT DEFAULT ''",
}

STOCK_CATALOG_COLUMNS = {
    "symbol": "symbol TEXT DEFAULT ''",
    "name": "name TEXT DEFAULT ''",
    "market": "market TEXT DEFAULT ''",
    "exchange": "exchange TEXT DEFAULT ''",
    "board": "board TEXT DEFAULT ''",
    "industry": "industry TEXT DEFAULT ''",
    "area": "area TEXT DEFAULT ''",
    "list_status": "list_status TEXT DEFAULT 'L'",
    "list_date": "list_date TEXT DEFAULT ''",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

STOCK_QUOTE_COLUMNS = {
    "symbol": "symbol TEXT DEFAULT ''",
    "price": "price REAL",
    "pre_close": "pre_close REAL",
    "change": "change REAL",
    "change_pct": "change_pct REAL",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "volume": "volume REAL DEFAULT 0",
    "amount": "amount REAL",
    "turnover_rate": "turnover_rate REAL",
    "quote_time": "quote_time TEXT DEFAULT ''",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# v13（B2）：信号台账。每个 (symbol, trade_date, source, provisional) 一行，记录
# 当时的投票决策、（D 阶段）模型输出，以及用 B1 标签回填的前瞻净收益/下行风险。
# label_status: pending（待回填）| filled（已回填）| expired（永不回填）| bad_data（决策日缺 bar）。
# 与 backtest_results（run 粒度）、paper_ledger（fill 粒度）通过 backtest_id/symbol/trade_date
# 松耦合，不合并。详见 docs/prediction-target.md。
SIGNAL_LEDGER_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT",
    "trade_date": "trade_date TEXT",
    "source": "source TEXT DEFAULT 'monitor'",
    "provisional": "provisional INTEGER DEFAULT 0",
    "profile_name": "profile_name TEXT",
    "params_json": "params_json TEXT",
    "vote_threshold": "vote_threshold INTEGER",
    "recommendation": "recommendation TEXT",
    "buy_count": "buy_count INTEGER",
    "sell_count": "sell_count INTEGER",
    "components_json": "components_json TEXT",
    "features_json": "features_json TEXT",
    "model_id": "model_id TEXT",
    "p_win": "p_win REAL",
    "expected_ret": "expected_ret REAL",
    "downside_mae": "downside_mae REAL",
    "entry_price": "entry_price REAL",
    "exit_price": "exit_price REAL",
    "horizon": "horizon INTEGER DEFAULT 5",
    "fwd_net_return": "fwd_net_return REAL",
    "fwd_ret_aux": "fwd_ret_aux REAL",
    "fwd_mae": "fwd_mae REAL",
    "label_status": "label_status TEXT DEFAULT 'pending'",
    "data_version": "data_version TEXT",
    "backtest_id": "backtest_id INTEGER",
    "filled_at": "filled_at TIMESTAMP",
    "created_at": "created_at TIMESTAMP",
}

# v13（B2）：通用键值表，monitor 收盘例程记录"上次执行日"等状态，防重启重复跑。
KV_STORE_COLUMNS = {
    "key": "key TEXT",
    "value": "value TEXT",
    "updated_at": "updated_at TIMESTAMP",
}

# v14（C1）：指数日线落库。每个 (index_code, trade_date) 一行，供基准对比与市场
# 特征复用——此前指数历史从不落库，benchmark 每次回测都实时拉 Tushare（离线即降级）。
# index_code 用 tushare 风格代码（000300.SH / 000001.SH / 399001.SZ / 399006.SZ）。
INDEX_DAILY_COLUMNS = {
    "id": "id INTEGER",
    "index_code": "index_code TEXT",
    "trade_date": "trade_date TEXT",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "pct_chg": "pct_chg REAL",
    "amount": "amount REAL",
    "volume": "volume REAL DEFAULT 0",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# v14（C2）：市场宽度按交易日积累。无免费历史宽度 API，故走"增量积累制"——
# monitor 收盘例程 / `data refresh-market` 用当日 stock_quotes 终态快照聚合写入。
# 涨跌停家数用 A7 price_limit_for_symbol 分板块判定（替代旧的 ±9.8% 一刀切）。
# trade_date 为主键：每个交易日一行，重复刷新 upsert 覆盖。
MARKET_DAILY_COLUMNS = {
    "trade_date": "trade_date TEXT",
    "advancers": "advancers INTEGER DEFAULT 0",
    "decliners": "decliners INTEGER DEFAULT 0",
    "unchanged": "unchanged INTEGER DEFAULT 0",
    "limit_up": "limit_up INTEGER DEFAULT 0",
    "limit_down": "limit_down INTEGER DEFAULT 0",
    "total_amount": "total_amount REAL DEFAULT 0",
    "up_ratio": "up_ratio REAL",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# v14（C3）：行业板块基建。东财（akshare）为唯一主源（tushare 120 积分无免费行业指数
# 历史）。三张表：industry_boards（板块登记）、industry_board_bars（板块日线）、
# industry_membership（成分股最新快照 + as_of 观察日）。失败板块的特征组在 D 阶段自动
# NaN 降级，绝不造假。board_code 用东财板块代码（如 BK0475）。
INDUSTRY_BOARDS_COLUMNS = {
    "board_code": "board_code TEXT",
    "board_name": "board_name TEXT",
    "source": "source TEXT DEFAULT 'em'",
    "updated_at": "updated_at TIMESTAMP",
}

INDUSTRY_BOARD_BARS_COLUMNS = {
    "id": "id INTEGER",
    "board_code": "board_code TEXT",
    "trade_date": "trade_date TEXT",
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "pct_chg": "pct_chg REAL",
    "amount": "amount REAL",
    "turnover_rate": "turnover_rate REAL",
    "source": "source TEXT DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

# PK(board_code, symbol)：每个板块对每只成分股保留一行"最新快照"，as_of 记录观察日。
# 现状单快照（非逐日 point-in-time），D 阶段读取时按 as_of<=trade_date 取最新并标
# industry_point_in_time=False 前视警告（诚实标注局限）。
INDUSTRY_MEMBERSHIP_COLUMNS = {
    "board_code": "board_code TEXT",
    "symbol": "symbol TEXT",
    "as_of": "as_of TEXT",
    "source": "source TEXT DEFAULT 'em'",
    "updated_at": "updated_at TIMESTAMP",
}

# v15（D2）：ML 数据集 manifest 落库（dataset_id 主键；列表/字典字段 JSON 编码）。
# 与 ml_models（D3）同属 v15——D2 先建 ml_datasets，D3 再增 ml_models，不另起版本号。
ML_DATASETS_COLUMNS = {
    "dataset_id": "dataset_id TEXT",
    "n_rows": "n_rows INTEGER NOT NULL DEFAULT 0",
    "n_positive": "n_positive INTEGER NOT NULL DEFAULT 0",
    "positive_rate": "positive_rate REAL",
    "start_date": "start_date TEXT",
    "end_date": "end_date TEXT",
    "max_trade_date": "max_trade_date TEXT",
    "horizon": "horizon INTEGER NOT NULL DEFAULT 5",
    "aux_horizon": "aux_horizon INTEGER NOT NULL DEFAULT 10",
    "index_code": "index_code TEXT",
    "industry_point_in_time": "industry_point_in_time INTEGER NOT NULL DEFAULT 0",
    "csv_path": "csv_path TEXT",
    "symbols_json": "symbols_json TEXT",
    "rejected_json": "rejected_json TEXT",
    "groups_json": "groups_json TEXT",
    "feature_columns_json": "feature_columns_json TEXT",
    "cost_model_json": "cost_model_json TEXT",
    "profile_snapshot_json": "profile_snapshot_json TEXT",
    "data_versions_json": "data_versions_json TEXT",
    "warnings_json": "warnings_json TEXT",
    "created_at": "created_at TIMESTAMP",
    "updated_at": "updated_at TIMESTAMP",
}

ML_MODELS_COLUMNS = {
    "model_id": "model_id TEXT",
    "kind": "kind TEXT",
    "target": "target TEXT",
    "horizon": "horizon INTEGER NOT NULL DEFAULT 5",
    "dataset_id": "dataset_id TEXT",
    "n_features": "n_features INTEGER NOT NULL DEFAULT 0",
    "threshold": "threshold REAL",
    "oos_brier": "oos_brier REAL",
    "oos_auc": "oos_auc REAL",
    "oos_logloss": "oos_logloss REAL",
    "oos_ece": "oos_ece REAL",
    "oos_mae": "oos_mae REAL",
    "oos_r2": "oos_r2 REAL",
    "base_rate": "base_rate REAL",
    "base_rate_brier": "base_rate_brier REAL",
    "coverage_at_threshold": "coverage_at_threshold REAL",
    "win_rate_at_threshold": "win_rate_at_threshold REAL",
    "n_oos": "n_oos INTEGER NOT NULL DEFAULT 0",
    "n_train": "n_train INTEGER NOT NULL DEFAULT 0",
    "n_rows": "n_rows INTEGER NOT NULL DEFAULT 0",
    "n_positive": "n_positive INTEGER NOT NULL DEFAULT 0",
    "max_trade_date": "max_trade_date TEXT",
    "status": "status TEXT NOT NULL DEFAULT 'candidate'",
    "stale": "stale INTEGER NOT NULL DEFAULT 0",
    "artifact_path": "artifact_path TEXT",
    "sklearn_version": "sklearn_version TEXT",
    "feature_groups_json": "feature_groups_json TEXT",
    "selected_params_json": "selected_params_json TEXT",
    "metrics_json": "metrics_json TEXT",
    "warnings_json": "warnings_json TEXT",
    "trained_at": "trained_at TIMESTAMP",
    "created_at": "created_at TIMESTAMP",
    "updated_at": "updated_at TIMESTAMP",
}

# v16（期货 P1）：合约元数据 / K 线 / 报价快照 / 通知去重。
# 期货与股票的交易规则、账户核算分别实现（roadmap §6），故独立成表不混用 daily_bars。
FUTURES_CONTRACT_COLUMNS = {
    "symbol": "symbol TEXT PRIMARY KEY",  # canonical，如 RB2610.SHFE
    "product": "product TEXT NOT NULL DEFAULT ''",
    "exchange": "exchange TEXT NOT NULL DEFAULT ''",
    "name": "name TEXT NOT NULL DEFAULT ''",
    "multiplier": "multiplier REAL NOT NULL DEFAULT 0",
    "tick_size": "tick_size REAL NOT NULL DEFAULT 0",
    "night_start": "night_start TEXT NOT NULL DEFAULT ''",
    "night_end": "night_end TEXT NOT NULL DEFAULT ''",
    "listed_date": "listed_date TEXT NOT NULL DEFAULT ''",
    # DCE 合约表接口不可用（P0 §2），到期日为近似口径时 expiry_is_approximate=1
    "expiry_date": "expiry_date TEXT NOT NULL DEFAULT ''",
    "expiry_is_approximate": "expiry_is_approximate INTEGER NOT NULL DEFAULT 0",
    "rule_version": "rule_version TEXT NOT NULL DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

FUTURES_BAR_COLUMNS = {
    "id": "id INTEGER",
    "symbol": "symbol TEXT NOT NULL",
    "timeframe": "timeframe TEXT NOT NULL DEFAULT '1d'",  # '1d' | '60m'
    # 夜盘 bar 已按 P0 §4.2 规则归属到交易日（不信任新浪标签日期）
    "trade_date": "trade_date TEXT NOT NULL DEFAULT ''",
    "bar_time": "bar_time TEXT NOT NULL DEFAULT ''",  # '60m' 结束时刻；日线为 ''
    "open": "open REAL",
    "high": "high REAL",
    "low": "low REAL",
    "close": "close REAL",
    "volume": "volume REAL NOT NULL DEFAULT 0",
    "hold": "hold REAL NOT NULL DEFAULT 0",
    "settle": "settle REAL",
    "source": "source TEXT NOT NULL DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

FUTURES_QUOTE_COLUMNS = {
    "symbol": "symbol TEXT PRIMARY KEY",
    "price": "price REAL",
    "upper_limit": "upper_limit REAL",
    "lower_limit": "lower_limit REAL",
    "margin_per_hand": "margin_per_hand REAL",
    # 1=用了 futures_meta 缺省费率估算（快照缺失），展示须注明近似
    "margin_is_estimate": "margin_is_estimate INTEGER NOT NULL DEFAULT 0",
    "fee_per_lot": "fee_per_lot REAL",
    "is_main": "is_main INTEGER NOT NULL DEFAULT 0",
    "price_time": "price_time TEXT NOT NULL DEFAULT ''",
    "source": "source TEXT NOT NULL DEFAULT ''",
    "updated_at": "updated_at TIMESTAMP",
}

NOTIFY_LOG_COLUMNS = {
    # 去重键 = f"{合约}|{周期}|{信号时间}|{策略版本}|{kind}"（roadmap P1：重启不重复通知）
    "dedup_key": "dedup_key TEXT PRIMARY KEY",
    "channel": "channel TEXT NOT NULL DEFAULT ''",
    "kind": "kind TEXT NOT NULL DEFAULT ''",
    "payload_json": "payload_json TEXT NOT NULL DEFAULT ''",
    "sent_at": "sent_at TIMESTAMP",
}

# v17（期货 P2）：订单 / 成交 / 账户逐日快照——「能重现报告」的审计底账。
# 列名用 open_close 而非 offset（OFFSET 是 SQLite 保留字，避免全链路引号转义）。
FUTURES_ORDER_COLUMNS = {
    "id": "id INTEGER PRIMARY KEY",
    "run_id": "run_id TEXT NOT NULL DEFAULT ''",  # 一次回测运行一个 id（报告重放键）
    "symbol": "symbol TEXT NOT NULL",
    "direction": "direction TEXT NOT NULL",  # LONG | SHORT
    "open_close": "open_close TEXT NOT NULL",  # OPEN | CLOSE | CLOSE_TODAY | CLOSE_YESTERDAY
    "lots": "lots INTEGER NOT NULL",
    "price": "price REAL",  # None = 市价单（下一可交易时点撮合）
    "status": "status TEXT NOT NULL",  # PENDING/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED
    "filled_lots": "filled_lots INTEGER NOT NULL DEFAULT 0",
    "avg_fill_price": "avg_fill_price REAL",
    "trade_date": "trade_date TEXT NOT NULL DEFAULT ''",
    "bar_time": "bar_time TEXT NOT NULL DEFAULT ''",
    # 拒单/撤单原因：limit_up|volume_cap|fee_missing|margin_shortfall|close_overdraft…
    "reason": "reason TEXT NOT NULL DEFAULT ''",
    "created_at": "created_at TIMESTAMP",
    "updated_at": "updated_at TIMESTAMP",
}

FUTURES_TRADE_COLUMNS = {
    "id": "id INTEGER PRIMARY KEY AUTOINCREMENT",
    "run_id": "run_id TEXT NOT NULL DEFAULT ''",
    "order_id": "order_id TEXT NOT NULL DEFAULT ''",
    "symbol": "symbol TEXT NOT NULL",
    "direction": "direction TEXT NOT NULL",
    "open_close": "open_close TEXT NOT NULL",
    "price": "price REAL NOT NULL",
    "lots": "lots INTEGER NOT NULL",
    "fee": "fee REAL NOT NULL DEFAULT 0",
    "trade_date": "trade_date TEXT NOT NULL DEFAULT ''",
    "bar_time": "bar_time TEXT NOT NULL DEFAULT ''",
    "realized_pnl": "realized_pnl REAL",  # 平仓成交的已实现盈亏；开仓为 NULL
    # plain CLOSE 跨今昨拆分（P2 验收「逐笔核对」要能对上账本 CloseBreakdown）
    "close_from_yesterday": "close_from_yesterday INTEGER",
    "close_from_today": "close_from_today INTEGER",
    "created_at": "created_at TIMESTAMP",
}

FUTURES_ACCOUNT_DAILY_COLUMNS = {
    "run_id": "run_id TEXT NOT NULL",
    "trade_date": "trade_date TEXT NOT NULL",
    # 结算后 balance == equity 是逐日盯市不变式；盘中另存快照时两者可分离
    "balance": "balance REAL NOT NULL DEFAULT 0",
    "equity": "equity REAL",
    "available": "available REAL",
    "margin_occupied": "margin_occupied REAL NOT NULL DEFAULT 0",
    "realized_pnl_today": "realized_pnl_today REAL NOT NULL DEFAULT 0",
    "position_pnl_today": "position_pnl_today REAL NOT NULL DEFAULT 0",
    "fees_today": "fees_today REAL NOT NULL DEFAULT 0",
    "exposure_value": "exposure_value REAL",  # 名义敞口（多空合计绝对值）
    "settlement_errors": "settlement_errors TEXT NOT NULL DEFAULT ''",  # JSON 数组
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


def init_database(path: Path | None = None, *, force: bool = False) -> Path:
    """确保 schema 就绪并返回库路径。

    进程内 init-once：同一进程对同一路径只有首次调用会真正跑 DDL/迁移
    （全量 schema 引导约 628 行 + 每表 PRAGMA + 全库 integrity_check 是
    一次性的引导成本，CRUD 每次重跑是纯浪费）。schema 不会被本进程改坏：
    引导是幂等的，且 ``_INIT_DONE`` 只在引导成功后登记。
    逃生口：``force=True`` 强制重跑（升级/测试 DROP 表后重建场景）。
    跨进程语义不变：每个进程首访各跑一次，与现状一致。
    """
    target = path or database_path()
    resolved = target.resolve()
    if not force and resolved in _INIT_DONE:
        return target
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
                system_key TEXT,
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
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_strategies_system_key "
            "ON strategies(system_key) WHERE system_key IS NOT NULL"
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
                default_strategy_id INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(default_strategy_id) REFERENCES strategies(id) ON DELETE SET NULL,
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
                exchange TEXT NOT NULL DEFAULT '',
                board TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                area TEXT NOT NULL DEFAULT '',
                list_status TEXT NOT NULL DEFAULT 'L',
                list_date TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "stock_catalog", STOCK_CATALOG_COLUMNS)
        connection.execute(
            "UPDATE stock_catalog SET board = market "
            "WHERE (board IS NULL OR board = '') AND market <> ''"
        )
        connection.execute(
            """
            UPDATE stock_catalog
            SET exchange = CASE
                WHEN symbol LIKE '%.SH' THEN 'SSE'
                WHEN symbol LIKE '%.SZ' THEN 'SZSE'
                WHEN symbol LIKE '%.BJ' THEN 'BSE'
                ELSE exchange
            END
            WHERE exchange IS NULL OR exchange = ''
            """
        )
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
                pre_close REAL,
                change REAL,
                pct_chg REAL,
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_quotes (
                symbol TEXT PRIMARY KEY,
                price REAL NOT NULL,
                pre_close REAL,
                change REAL,
                change_pct REAL,
                open REAL,
                high REAL,
                low REAL,
                volume REAL NOT NULL DEFAULT 0,
                amount REAL,
                turnover_rate REAL,
                quote_time TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "stock_quotes", STOCK_QUOTE_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_stock_quotes_time "
            "ON stock_quotes(quote_time DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'monitor'
                    CHECK(source IN ('monitor', 'backtest', 'dataset', 'manual')),
                provisional INTEGER NOT NULL DEFAULT 0 CHECK(provisional IN (0, 1)),
                profile_name TEXT,
                params_json TEXT,
                vote_threshold INTEGER,
                recommendation TEXT,
                buy_count INTEGER,
                sell_count INTEGER,
                components_json TEXT,
                features_json TEXT,
                model_id TEXT,
                p_win REAL,
                expected_ret REAL,
                downside_mae REAL,
                entry_price REAL,
                exit_price REAL,
                horizon INTEGER NOT NULL DEFAULT 5,
                fwd_net_return REAL,
                fwd_ret_aux REAL,
                fwd_mae REAL,
                label_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(label_status IN ('pending', 'filled', 'expired', 'bad_data')),
                data_version TEXT,
                backtest_id INTEGER,
                filled_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, trade_date, source, provisional)
            )
            """
        )
        _ensure_columns(connection, "signal_ledger", SIGNAL_LEDGER_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ledger_status "
            "ON signal_ledger(label_status, symbol)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ledger_symbol_date "
            "ON signal_ledger(symbol, trade_date DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ledger_backtest "
            "ON signal_ledger(backtest_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "kv_store", KV_STORE_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS index_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                index_code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pct_chg REAL,
                amount REAL,
                volume REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(index_code, trade_date)
            )
            """
        )
        _ensure_columns(connection, "index_daily", INDEX_DAILY_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_index_daily_code_date "
            "ON index_daily(index_code, trade_date)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_daily (
                trade_date TEXT PRIMARY KEY,
                advancers INTEGER NOT NULL DEFAULT 0,
                decliners INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                limit_up INTEGER NOT NULL DEFAULT 0,
                limit_down INTEGER NOT NULL DEFAULT 0,
                total_amount REAL NOT NULL DEFAULT 0,
                up_ratio REAL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "market_daily", MARKET_DAILY_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_daily_date "
            "ON market_daily(trade_date DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_boards (
                board_code TEXT PRIMARY KEY,
                board_name TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'em',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "industry_boards", INDUSTRY_BOARDS_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_board_bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                pct_chg REAL,
                amount REAL,
                turnover_rate REAL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(board_code, trade_date)
            )
            """
        )
        _ensure_columns(connection, "industry_board_bars", INDUSTRY_BOARD_BARS_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_industry_board_bars_code_date "
            "ON industry_board_bars(board_code, trade_date)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_membership (
                board_code TEXT NOT NULL,
                symbol TEXT NOT NULL,
                as_of TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'em',
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(board_code, symbol)
            )
            """
        )
        _ensure_columns(connection, "industry_membership", INDUSTRY_MEMBERSHIP_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_industry_membership_symbol "
            "ON industry_membership(symbol)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ml_datasets (
                dataset_id TEXT PRIMARY KEY,
                n_rows INTEGER NOT NULL DEFAULT 0,
                n_positive INTEGER NOT NULL DEFAULT 0,
                positive_rate REAL,
                start_date TEXT,
                end_date TEXT,
                max_trade_date TEXT,
                horizon INTEGER NOT NULL DEFAULT 5,
                aux_horizon INTEGER NOT NULL DEFAULT 10,
                index_code TEXT,
                industry_point_in_time INTEGER NOT NULL DEFAULT 0,
                csv_path TEXT,
                symbols_json TEXT,
                rejected_json TEXT,
                groups_json TEXT,
                feature_columns_json TEXT,
                cost_model_json TEXT,
                profile_snapshot_json TEXT,
                data_versions_json TEXT,
                warnings_json TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "ml_datasets", ML_DATASETS_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ml_models (
                model_id TEXT PRIMARY KEY,
                kind TEXT,
                target TEXT,
                horizon INTEGER NOT NULL DEFAULT 5,
                dataset_id TEXT,
                n_features INTEGER NOT NULL DEFAULT 0,
                threshold REAL,
                oos_brier REAL,
                oos_auc REAL,
                oos_logloss REAL,
                oos_ece REAL,
                oos_mae REAL,
                oos_r2 REAL,
                base_rate REAL,
                base_rate_brier REAL,
                coverage_at_threshold REAL,
                win_rate_at_threshold REAL,
                n_oos INTEGER NOT NULL DEFAULT 0,
                n_train INTEGER NOT NULL DEFAULT 0,
                n_rows INTEGER NOT NULL DEFAULT 0,
                n_positive INTEGER NOT NULL DEFAULT 0,
                max_trade_date TEXT,
                status TEXT NOT NULL DEFAULT 'candidate',
                stale INTEGER NOT NULL DEFAULT 0,
                artifact_path TEXT,
                sklearn_version TEXT,
                feature_groups_json TEXT,
                selected_params_json TEXT,
                metrics_json TEXT,
                warnings_json TEXT,
                trained_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "ml_models", ML_MODELS_COLUMNS)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ml_models_status ON ml_models(status, target)"
        )
        # ── v16（期货 P1）───────────────────────────────────────────────
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_contracts (
                symbol TEXT PRIMARY KEY,
                product TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                multiplier REAL NOT NULL DEFAULT 0,
                tick_size REAL NOT NULL DEFAULT 0,
                night_start TEXT NOT NULL DEFAULT '',
                night_end TEXT NOT NULL DEFAULT '',
                listed_date TEXT NOT NULL DEFAULT '',
                expiry_date TEXT NOT NULL DEFAULT '',
                expiry_is_approximate INTEGER NOT NULL DEFAULT 0,
                rule_version TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "futures_contracts", FUTURES_CONTRACT_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_bars (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL DEFAULT '1d',
                trade_date TEXT NOT NULL DEFAULT '',
                bar_time TEXT NOT NULL DEFAULT '',
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL NOT NULL DEFAULT 0,
                hold REAL NOT NULL DEFAULT 0,
                settle REAL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP,
                UNIQUE(symbol, timeframe, trade_date, bar_time)
            )
            """
        )
        _ensure_columns(connection, "futures_bars", FUTURES_BAR_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_quotes (
                symbol TEXT PRIMARY KEY,
                price REAL,
                upper_limit REAL,
                lower_limit REAL,
                margin_per_hand REAL,
                margin_is_estimate INTEGER NOT NULL DEFAULT 0,
                fee_per_lot REAL,
                is_main INTEGER NOT NULL DEFAULT 0,
                price_time TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "futures_quotes", FUTURES_QUOTE_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_log (
                dedup_key TEXT PRIMARY KEY,
                channel TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '',
                sent_at TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "notify_log", NOTIFY_LOG_COLUMNS)
        # ── v17（期货 P2）───────────────────────────────────────────────
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_orders (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                open_close TEXT NOT NULL,
                lots INTEGER NOT NULL,
                price REAL,
                status TEXT NOT NULL,
                filled_lots INTEGER NOT NULL DEFAULT 0,
                avg_fill_price REAL,
                trade_date TEXT NOT NULL DEFAULT '',
                bar_time TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "futures_orders", FUTURES_ORDER_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                open_close TEXT NOT NULL,
                price REAL NOT NULL,
                lots INTEGER NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                trade_date TEXT NOT NULL DEFAULT '',
                bar_time TEXT NOT NULL DEFAULT '',
                realized_pnl REAL,
                close_from_yesterday INTEGER,
                close_from_today INTEGER,
                created_at TIMESTAMP
            )
            """
        )
        _ensure_columns(connection, "futures_trades", FUTURES_TRADE_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS futures_account_daily (
                run_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                balance REAL NOT NULL DEFAULT 0,
                equity REAL,
                available REAL,
                margin_occupied REAL NOT NULL DEFAULT 0,
                realized_pnl_today REAL NOT NULL DEFAULT 0,
                position_pnl_today REAL NOT NULL DEFAULT 0,
                fees_today REAL NOT NULL DEFAULT 0,
                exposure_value REAL,
                settlement_errors TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP,
                PRIMARY KEY (run_id, trade_date)
            )
            """
        )
        _ensure_columns(connection, "futures_account_daily", FUTURES_ACCOUNT_DAILY_COLUMNS)
        connection.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}")

    _INIT_DONE.add(resolved)
    return target


def check_database_integrity(path: Path | None = None) -> Path:
    """显式全库完整性校验（容器启动/部署升级时调用）。

    ``PRAGMA integrity_check`` 是全库扫描，随 init-once 从常规引导中移出——
    原先每次 CRUD 都会跑一遍，对大库是秒级纯开销。部署脚本
    （docker-entrypoint / update_from_github.sh）已在每次启动/升级时显式
    跑 ``python -m ripple_tradePilot.storage``，校验频率从"每次读写"合理化为
    "每次部署"，且对损坏库的检测时机（部署时 fail-fast）反而更早暴露问题。
    """
    target = path or database_path()
    with sqlite3.connect(target, timeout=30) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {target}: {integrity}")
    return target


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


# ---------------------------------------------------------------------------
# v15（D2）：ML 数据集 manifest 落库
# ---------------------------------------------------------------------------
# manifest 中列表/字典字段以 JSON 文本存储；register 序列化、load/list 反序列化，
# 调用方（ml.dataset）始终拿到/传入原生 Python 对象。
_DATASET_JSON_FIELDS = (
    "symbols",
    "rejected",
    "groups",
    "feature_columns",
    "cost_model",
    "profile_snapshot",
    "data_versions",
    "warnings",
)


def register_dataset(manifest: Mapping[str, Any], path: Path | None = None) -> str:
    """写入/更新 ``ml_datasets``（D2）。按 ``dataset_id`` upsert 幂等，返回 dataset_id。

    ``manifest`` 的列表/字典字段（symbols/rejected/groups/feature_columns/cost_model/
    profile_snapshot/data_versions/warnings）以 JSON 编码落库；标量字段直接存。
    """
    dataset_id = str(manifest["dataset_id"])

    def _dump(key: str) -> str:
        return json.dumps(manifest.get(key), ensure_ascii=False, default=str)

    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO ml_datasets (
                dataset_id, n_rows, n_positive, positive_rate,
                start_date, end_date, max_trade_date, horizon, aux_horizon,
                index_code, industry_point_in_time, csv_path,
                symbols_json, rejected_json, groups_json, feature_columns_json,
                cost_model_json, profile_snapshot_json, data_versions_json,
                warnings_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(dataset_id) DO UPDATE SET
                n_rows = excluded.n_rows,
                n_positive = excluded.n_positive,
                positive_rate = excluded.positive_rate,
                start_date = excluded.start_date,
                end_date = excluded.end_date,
                max_trade_date = excluded.max_trade_date,
                horizon = excluded.horizon,
                aux_horizon = excluded.aux_horizon,
                index_code = excluded.index_code,
                industry_point_in_time = excluded.industry_point_in_time,
                csv_path = excluded.csv_path,
                symbols_json = excluded.symbols_json,
                rejected_json = excluded.rejected_json,
                groups_json = excluded.groups_json,
                feature_columns_json = excluded.feature_columns_json,
                cost_model_json = excluded.cost_model_json,
                profile_snapshot_json = excluded.profile_snapshot_json,
                data_versions_json = excluded.data_versions_json,
                warnings_json = excluded.warnings_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                dataset_id,
                int(manifest.get("n_rows", 0) or 0),
                int(manifest.get("n_positive", 0) or 0),
                manifest.get("positive_rate"),
                manifest.get("start_date"),
                manifest.get("end_date"),
                manifest.get("max_trade_date"),
                int(manifest.get("horizon", 5) or 5),
                int(manifest.get("aux_horizon", 10) or 10),
                manifest.get("index_code"),
                1 if manifest.get("industry_point_in_time") else 0,
                manifest.get("csv_path"),
                _dump("symbols"),
                _dump("rejected"),
                _dump("groups"),
                _dump("feature_columns"),
                _dump("cost_model"),
                _dump("profile_snapshot"),
                _dump("data_versions"),
                _dump("warnings"),
            ),
        )
    return dataset_id


def _row_to_dataset_manifest(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ml_datasets 行解码为原生 manifest dict（JSON 字段反序列化）。"""
    data = dict(row)  # sqlite3.Row 无 .get()，先转 dict

    def _load(key: str) -> Any:
        raw = data.get(f"{key}_json")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    manifest: Dict[str, Any] = {
        "dataset_id": data["dataset_id"],
        "n_rows": data["n_rows"],
        "n_positive": data["n_positive"],
        "positive_rate": data["positive_rate"],
        "start_date": data["start_date"],
        "end_date": data["end_date"],
        "max_trade_date": data["max_trade_date"],
        "horizon": data["horizon"],
        "aux_horizon": data["aux_horizon"],
        "index_code": data["index_code"],
        "industry_point_in_time": bool(data["industry_point_in_time"]),
        "csv_path": data["csv_path"],
        "created_at": data.get("created_at"),
    }
    for key in _DATASET_JSON_FIELDS:
        manifest[key] = _load(key)
    return manifest


def load_dataset_manifest(
    dataset_id: str, path: Path | None = None
) -> Optional[Dict[str, Any]]:
    """读取某数据集 manifest（JSON 字段解码）；不存在返回 ``None``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM ml_datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    return _row_to_dataset_manifest(row) if row else None


def list_datasets(path: Path | None = None) -> List[Dict[str, Any]]:
    """列出全部数据集 manifest（按 created_at 降序、再按 dataset_id），JSON 字段解码。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM ml_datasets ORDER BY created_at DESC, dataset_id DESC"
        ).fetchall()
    return [_row_to_dataset_manifest(row) for row in rows]


_MODEL_JSON_FIELDS = (
    "feature_groups",
    "selected_params",
    "metrics",
    "warnings",
)

# 标量列（与 ML_MODELS_COLUMNS 对应，JSON 列单独处理）
_MODEL_SCALAR_FIELDS = (
    "model_id", "kind", "target", "horizon", "dataset_id", "n_features", "threshold",
    "oos_brier", "oos_auc", "oos_logloss", "oos_ece", "oos_mae", "oos_r2",
    "base_rate", "base_rate_brier", "coverage_at_threshold", "win_rate_at_threshold",
    "n_oos", "n_train", "n_rows", "n_positive", "max_trade_date",
    "status", "stale", "artifact_path", "sklearn_version", "trained_at",
)


def register_model(model: Mapping[str, Any], path: Path | None = None) -> str:
    """写入/更新 ``ml_models``（D3）。按 ``model_id`` upsert 幂等，返回 model_id。

    ``model`` 的 list/dict 字段（feature_groups/selected_params/metrics/warnings）以 JSON
    编码落库；标量字段直接存。新建默认 ``status='candidate'``（调用方可显式覆盖）。
    """
    model_id = str(model["model_id"])

    def _dump(key: str) -> str:
        return json.dumps(model.get(key), ensure_ascii=False, default=str)

    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO ml_models (
                model_id, kind, target, horizon, dataset_id, n_features, threshold,
                oos_brier, oos_auc, oos_logloss, oos_ece, oos_mae, oos_r2,
                base_rate, base_rate_brier, coverage_at_threshold, win_rate_at_threshold,
                n_oos, n_train, n_rows, n_positive, max_trade_date,
                status, stale, artifact_path, sklearn_version,
                feature_groups_json, selected_params_json, metrics_json, warnings_json,
                trained_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT(model_id) DO UPDATE SET
                kind = excluded.kind,
                target = excluded.target,
                horizon = excluded.horizon,
                dataset_id = excluded.dataset_id,
                n_features = excluded.n_features,
                threshold = excluded.threshold,
                oos_brier = excluded.oos_brier,
                oos_auc = excluded.oos_auc,
                oos_logloss = excluded.oos_logloss,
                oos_ece = excluded.oos_ece,
                oos_mae = excluded.oos_mae,
                oos_r2 = excluded.oos_r2,
                base_rate = excluded.base_rate,
                base_rate_brier = excluded.base_rate_brier,
                coverage_at_threshold = excluded.coverage_at_threshold,
                win_rate_at_threshold = excluded.win_rate_at_threshold,
                n_oos = excluded.n_oos,
                n_train = excluded.n_train,
                n_rows = excluded.n_rows,
                n_positive = excluded.n_positive,
                max_trade_date = excluded.max_trade_date,
                status = excluded.status,
                stale = excluded.stale,
                artifact_path = excluded.artifact_path,
                sklearn_version = excluded.sklearn_version,
                feature_groups_json = excluded.feature_groups_json,
                selected_params_json = excluded.selected_params_json,
                metrics_json = excluded.metrics_json,
                warnings_json = excluded.warnings_json,
                trained_at = excluded.trained_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                model_id,
                model.get("kind"),
                model.get("target"),
                int(model.get("horizon", 5) or 5),
                model.get("dataset_id"),
                int(model.get("n_features", 0) or 0),
                model.get("threshold"),
                model.get("oos_brier"),
                model.get("oos_auc"),
                model.get("oos_logloss"),
                model.get("oos_ece"),
                model.get("oos_mae"),
                model.get("oos_r2"),
                model.get("base_rate"),
                model.get("base_rate_brier"),
                model.get("coverage_at_threshold"),
                model.get("win_rate_at_threshold"),
                int(model.get("n_oos", 0) or 0),
                int(model.get("n_train", 0) or 0),
                int(model.get("n_rows", 0) or 0),
                int(model.get("n_positive", 0) or 0),
                model.get("max_trade_date"),
                str(model.get("status", "candidate")),
                1 if model.get("stale") else 0,
                model.get("artifact_path"),
                model.get("sklearn_version"),
                _dump("feature_groups"),
                _dump("selected_params"),
                _dump("metrics"),
                _dump("warnings"),
                model.get("trained_at"),
            ),
        )
    return model_id


def _row_to_model(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ml_models 行解码为原生 dict（JSON 字段反序列化）。"""
    data = dict(row)  # sqlite3.Row 无 .get()，先转 dict

    def _load(key: str) -> Any:
        raw = data.get(f"{key}_json")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    model: Dict[str, Any] = {field: data.get(field) for field in _MODEL_SCALAR_FIELDS}
    model["stale"] = bool(data.get("stale"))
    model["created_at"] = data.get("created_at")
    model["updated_at"] = data.get("updated_at")
    for key in _MODEL_JSON_FIELDS:
        model[key] = _load(key)
    return model


def load_model(model_id: str, path: Path | None = None) -> Optional[Dict[str, Any]]:
    """读取某模型行（JSON 字段解码）；不存在返回 ``None``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM ml_models WHERE model_id = ?", (model_id,)
        ).fetchone()
    return _row_to_model(row) if row else None


def list_models(
    path: Path | None = None, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """列出模型（按 trained_at 降序）；``status`` 非空时过滤。"""
    target = init_database(path)
    query = "SELECT * FROM ml_models"
    params: Tuple[Any, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY trained_at DESC, model_id DESC"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [_row_to_model(row) for row in rows]


def set_model_status(
    model_id: str,
    status: str,
    *,
    stale: Optional[bool] = None,
    warnings: Optional[Sequence[str]] = None,
    path: Path | None = None,
) -> None:
    """更新模型 ``status``（晋升/退役/标 demo），可同步 ``stale`` 与 ``warnings``。"""
    target = init_database(path)
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: List[Any] = [status]
    if stale is not None:
        sets.append("stale = ?")
        params.append(1 if stale else 0)
    if warnings is not None:
        sets.append("warnings_json = ?")
        params.append(json.dumps(list(warnings), ensure_ascii=False, default=str))
    params.append(model_id)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            f"UPDATE ml_models SET {', '.join(sets)} WHERE model_id = ?", tuple(params)
        )


def retire_promoted_models(
    target_name: str,
    horizon: int,
    *,
    exclude_model_id: Optional[str] = None,
    path: Path | None = None,
) -> int:
    """把同 (target, horizon) 的其它 promoted/demo 模型退役，保证每个目标只有一个在位。"""
    db = init_database(path)
    with sqlite3.connect(db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT model_id FROM ml_models WHERE target = ? AND horizon = ? "
            "AND status IN ('promoted', 'demo')",
            (target_name, int(horizon)),
        ).fetchall()
        retired = [
            r["model_id"] for r in rows if r["model_id"] != exclude_model_id
        ]
        for mid in retired:
            connection.execute(
                "UPDATE ml_models SET status = 'retired', updated_at = CURRENT_TIMESTAMP "
                "WHERE model_id = ?",
                (mid,),
            )
    return len(retired)


def load_promoted_model(
    target_name: Optional[str] = None,
    *,
    include_demo: bool = False,
    path: Path | None = None,
) -> Optional[Dict[str, Any]]:
    """取在位模型（``status='promoted'``，可选含 ``demo``）；无则 ``None``。

    多个时取 trained_at 最新的一个。``target_name`` 非空时按目标过滤（D5 scoring 默认
    取 ``win5`` 分类模型）。
    """
    db = init_database(path)
    statuses = ("promoted", "demo") if include_demo else ("promoted",)
    placeholders = ",".join("?" for _ in statuses)
    query = f"SELECT * FROM ml_models WHERE status IN ({placeholders})"
    params: List[Any] = list(statuses)
    if target_name:
        query += " AND target = ?"
        params.append(target_name)
    query += " ORDER BY trained_at DESC, model_id DESC LIMIT 1"
    with sqlite3.connect(db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(query, tuple(params)).fetchone()
    return _row_to_model(row) if row else None


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
