"""SQLite 存储统一 facade（自原 2700 行单模块按域拆分，P5）。

按域拆分为五个子模块，本文件保留为**永久 facade**：所有历史导入路径
（``from ripple_tradePilot.storage.database import X`` /
``from .database import X`` / ``import database as db``）继续生效，
存量测试零改动即为本拆分正确性的验收标准。

- ``schema``        版本 + DDL + 迁移 + init-once + 完整性校验
- ``market``        日线 / 指数 / 市场宽度 / 行业板块
- ``ml_store``      ML 数据集 manifest / 模型注册表
- ``catalog``       标的目录 / 实时报价快照
- ``futures_store`` 期货合约 / K 线 / 报价 / 通知 / 订单 / 成交 / 账户

新代码请直接从对应域模块导入；本 facade 永不删除（几十处存量导入依赖）。
"""

from __future__ import annotations

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
from .catalog import (
    list_stock_catalog,
    load_stock_quotes,
    stock_catalog_industries,
    stock_catalog_name,
    stock_catalog_names,
    upsert_stock_catalog,
    upsert_stock_quotes,
)
from .futures_store import (
    delete_notification,
    insert_futures_orders,
    insert_futures_trades,
    latest_futures_bar,
    list_futures_bar_symbols,
    load_futures_orders,
    load_futures_trades,
    list_notifications,
    load_futures_account_daily,
    load_futures_bars,
    load_futures_contracts,
    load_futures_orders,
    load_futures_quote,
    load_futures_quotes,
    load_futures_trades,
    record_notification,
    upsert_futures_account_daily,
    upsert_futures_bars,
    upsert_futures_contracts,
    upsert_futures_quotes,
)
