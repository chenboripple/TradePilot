"""SQLite schema 引导与完整性校验（自 storage/database.py 拆出，P5）。

内容：schema 版本常量、各表列定义（ALTER TABLE 增量补列用）、库路径解析、
``init_database`` 全量 DDL/迁移引导（进程内 init-once）与
``check_database_integrity`` 显式全库校验。

外部访问仍经 ``storage.database`` facade；本模块是 schema 域的唯一实现。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Mapping

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
