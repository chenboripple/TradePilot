"""期货回测入口层（roadmap P2 收尾）：库内扫描数据 → 引擎输入 → 跑完落库。

「能重现报告」的兑现：每次运行以 ``run_id`` 把订单/成交/账户逐日快照写进
v17 三张审计表（futures_orders / futures_trades / futures_account_daily），
汇总行写 backtest_results（run_kind='futures_backtest'），费用/保证金规则的
**近似口径**随行透出——不冒充官方费率。

数据来源只有 ``futures_bars``（由 ``tradepilot futures scan`` 落库）：本模块
不联网；无 60m 数据时抛 :class:`FuturesDataUnavailableError` 并指路 scan，
绝不静默降级。费率/保证金率推导（快照优先、品种规格兜底）全部 approximate：
``futures_comm_info`` 快照是「今天」的口径，套到历史区间本来就是近似，报告
必须如实标注。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..data.futures_meta import (
    PRODUCT_SPECS,
    ProductSpec,
    parse_contract_symbol,
    product_spec,
)
from ..storage.database import (
    insert_futures_orders,
    insert_futures_trades,
    list_futures_bar_symbols,
    load_futures_bars,
    load_futures_quote,
    upsert_futures_account_daily,
)
from ..storage.user_store import record_backtest_run
from .futures_engine import (
    ContractInput,
    EngineConfig,
    FuturesBacktestReport,
    build_roll_schedule,
    run_futures_backtest,
)
from .futures_rules import FeeRule, FeeSchedule, MarginRule, MarginSchedule
from .futures_walkforward import (
    FuturesWalkforwardReport,
    WalkforwardParams,
    futures_walkforward,
    main_timeline_signals,
)

__all__ = [
    "FuturesDataUnavailableError",
    "derive_schedules",
    "load_product_contracts",
    "run_product_backtest",
    "run_product_walkforward",
]


class FuturesDataUnavailableError(RuntimeError):
    """库内无该品种的 60m 数据（还没跑过 tradepilot futures scan）。"""


def load_product_contracts(
    product: str, path: Optional[Path] = None
) -> Dict[str, ContractInput]:
    """品种 → 全部真实合约的引擎输入（60m 信号/成交 + 日线结算/主力判定）。

    只收 60m 有数据的合约；日线缺失不致命（结算价缺失走引擎的
    settlement_errors 路径），但 60m 全空说明从未扫描过 → 显式报错。
    """
    spec = product_spec(product)
    if spec is None:
        raise ValueError(
            f"品种 {product} 不在首期范围（{sorted(PRODUCT_SPECS)}）")

    contracts: Dict[str, ContractInput] = {}
    for symbol in list_futures_bar_symbols("60m", path=path):
        # 精确到品种：startswith 会把 MA701 误收进 M；身份解析器只吃裸码
        # （RB2610），库内 canonical 带交易所后缀，先剥掉再判
        try:
            identity = parse_contract_symbol(symbol.split(".")[0])
        except ValueError:
            continue
        if identity.product != spec.code:
            continue
        bars_60m = load_futures_bars(symbol, "60m", path=path)
        if not bars_60m:
            continue
        daily = load_futures_bars(symbol, "1d", path=path)
        contracts[symbol] = ContractInput(
            symbol=symbol, multiplier=spec.multiplier, tick_size=spec.tick_size,
            bars_60m=[dict(row) for row in bars_60m],
            daily=[dict(row) for row in daily],
        )
    if not contracts:
        raise FuturesDataUnavailableError(
            f"库内无 {spec.code} 的 60m 数据：请先运行 `tradepilot futures scan` "
            "采集观察池 K 线（本命令不联网补数据）")
    return contracts


def derive_schedules(
    product: str,
    contracts: Dict[str, ContractInput],
    path: Optional[Path] = None,
) -> Tuple[FeeSchedule, MarginSchedule, List[str]]:
    """从 futures_quotes 快照推导费率/保证金率；快照缺失用品种规格兜底。

    两条路都是近似口径（快照是当日值、规格是期货公司口径缺省），规则一律
    ``approximate=True`` 并在返回的 notes 里写明来源——绝不冒充交易所公告。
    保证金率推导：margin_per_hand ÷ (price × 乘数)，用同一快照的价与每手
    保证金，自洽地还原成比率（引擎按「价×乘数×率」重算每手保证金）。
    """
    spec = product_spec(product)
    if spec is None:
        raise ValueError(f"品种 {product} 不在首期范围")

    quotes = []
    for symbol in contracts:
        quote = load_futures_quote(symbol, path=path)
        if quote is not None:
            quotes.append(dict(quote))
    # 主力快照优先（费率按品种一致，取代表性最强的那份）
    quotes.sort(key=lambda row: 0 if row.get("is_main") else 1)
    main_quote = quotes[0] if quotes else None

    fee_per_lot = None
    margin_rate = None
    fee_source = margin_source = "品种规格缺省（futures_meta PRODUCT_SPECS）"
    if main_quote:
        if (main_quote.get("fee_per_lot") or 0) > 0:
            fee_per_lot = float(main_quote["fee_per_lot"])
            fee_source = "futures_quotes 快照 fee_per_lot（单边近似）"
        price = main_quote.get("price") or 0
        per_hand = main_quote.get("margin_per_hand") or 0
        if price > 0 and per_hand > 0:
            candidate = per_hand / (price * spec.multiplier)
            if 0 < candidate <= 1:
                margin_rate = candidate
                margin_source = (
                    "futures_quotes 快照 margin_per_hand ÷ (price × 乘数)（近似）")

    if fee_per_lot is None:
        # 规格记的是往返手续费；引擎三档独立，按单边近似平摊（开/平今/平昨同价）
        fee_per_lot = spec.round_trip_fee / 2.0
    if margin_rate is None:
        margin_rate = spec.margin_rate

    earliest = min(
        str(bar.get("trade_date") or "99999999")
        for data in contracts.values() for bar in data.bars_60m
    )
    fees = FeeSchedule([FeeRule(
        effective_from=earliest, effective_to=None, approximate=True,
        open_per_lot=fee_per_lot, close_today_per_lot=fee_per_lot,
        close_yesterday_per_lot=fee_per_lot)])
    margins = MarginSchedule([MarginRule(
        effective_from=earliest, effective_to=None, rate=margin_rate,
        approximate=True)])
    notes = [f"手续费 {fee_per_lot:.2f} 元/手/边 ← {fee_source}",
             f"保证金率 {margin_rate:.2%} ← {margin_source}",
             "费率/保证金为快照近似口径，非交易所逐日公告规则"]
    return fees, margins, notes


def _persist_single(
    report: FuturesBacktestReport, spec: ProductSpec,
    config: EngineConfig, entry_window: int, notes: List[str],
    bar_count: int, path: Optional[Path],
) -> None:
    """审计三表 + backtest_results 汇总行（重放口径：按 run_id 全量可取回）。"""
    insert_futures_orders(report.order_rows(report.run_id), path)
    insert_futures_trades(report.trade_rows(report.run_id), path)
    upsert_futures_account_daily(report.daily_rows(report.run_id), path)
    metrics = dict(report.metrics)
    record_backtest_run(
        {
            "symbol": spec.code,
            "name": f"{spec.name}期货",
            "start_date": report.daily[0]["trade_date"] if report.daily else "",
            "end_date": report.daily[-1]["trade_date"] if report.daily else "",
            "initial_capital": config.initial_cash,
            "final_capital": metrics.get("final_equity", 0.0),
            "total_return": metrics.get("total_return", 0.0),
            "max_drawdown": metrics.get("max_drawdown", 0.0),
            "total_trades": metrics.get("fills", 0),
            "strategy_key": "futures_donchian",
            "bar_count": bar_count,
            "execution": "next_open",
            "result_json": json.dumps(
                {**metrics, "config": report.config_echo, "warnings": report.warnings},
                ensure_ascii=False, default=str),
        },
        path=path,
        run_kind="futures_backtest",
        params_json=json.dumps(
            {"entry_window": entry_window, **report.config_echo}, ensure_ascii=False),
        profile_source="futures_cli",
        report_json=json.dumps(
            {"run_id": report.run_id, "rolls": report.rolls, "notes": notes,
             "orders_by_status": metrics.get("orders_by_status", {})},
            ensure_ascii=False, default=str),
    )


def run_product_backtest(
    product: str,
    *,
    entry_window: int = 20,
    config: Optional[EngineConfig] = None,
    path: Optional[Path] = None,
    run_id: str = "",
    save: bool = True,
) -> Tuple[FuturesBacktestReport, List[str]]:
    """单次全时间线回测：换月计划 + 主力时间线信号（无前视），跑完落库。

    信号只在「该合约为当期主力」的交易日评估（:func:`main_timeline_signals`），
    换月由引擎在主力切换日显式平旧开新；评估窗口用合约自身完整历史。
    """
    contracts = load_product_contracts(product, path=path)
    spec = product_spec(product)
    assert spec is not None  # load_product_contracts 已校验
    fees, margins, notes = derive_schedules(product, contracts, path=path)

    if config is None:
        config = EngineConfig()
    if run_id:
        config = replace(config, run_id=run_id)
    elif not config.run_id:
        config = replace(config, run_id=_auto_run_id(spec.code))

    daily_by_symbol = {s: data.daily for s, data in contracts.items()}
    roll_schedule = build_roll_schedule(daily_by_symbol)
    main_by_date = dict(roll_schedule)
    signals = []
    for symbol, data in contracts.items():
        main_dates = [date for date, main in main_by_date.items() if main == symbol]
        signals.extend(main_timeline_signals(
            data.bars_60m, entry_window, symbol=symbol, main_dates=main_dates))
    report = run_futures_backtest(contracts, config, fees, margins, signals,
                                  roll_schedule)
    if save:
        bar_count = sum(len(data.bars_60m) for data in contracts.values())
        _persist_single(report, spec, config, entry_window, notes, bar_count, path)
    return report, notes


def run_product_walkforward(
    product: str,
    *,
    params: Optional[WalkforwardParams] = None,
    config: Optional[EngineConfig] = None,
    path: Optional[Path] = None,
    save: bool = True,
) -> Tuple[FuturesWalkforwardReport, List[str]]:
    """滚动样本外验证（锚定扩展训练 + 保留集），manifest 随汇总行落库。

    各分段是独立账户，审计三表不落（段是选参过程不是可重放报告）；落库的是
    manifest（数据范围/保留集起点/参数网格/预热/近似标记/策略版本）+ summary
    ——验收要求的「记录数据、参数和策略版本」正是这一层。
    """
    contracts = load_product_contracts(product, path=path)
    spec = product_spec(product)
    assert spec is not None
    fees, margins, notes = derive_schedules(product, contracts, path=path)
    if config is None:
        config = EngineConfig(run_id=_auto_run_id(spec.code, prefix="wff"))

    daily_by_symbol = {s: data.daily for s, data in contracts.items()}
    report = futures_walkforward(
        contracts, config, fees, margins, build_roll_schedule(daily_by_symbol),
        params=params)
    if save:
        record_backtest_run(
            {
                "symbol": spec.code,
                "name": f"{spec.name}期货（滚动样本外）",
                "start_date": report.manifest.get("data_start", ""),
                "end_date": report.manifest.get("data_end", ""),
                "initial_capital": config.initial_cash,
                "final_capital": config.initial_cash * (
                    1 + float(report.summary.get("oos_total_return") or 0)),
                "total_return": report.summary.get("oos_total_return", 0.0),
                "total_trades": report.summary.get("test_fills_total", 0),
                "strategy_key": "futures_donchian_walkforward",
                "execution": "next_open",
                "result_json": json.dumps(report.summary, ensure_ascii=False,
                                          default=str),
            },
            path=path,
            run_kind="futures_backtest_walkforward",
            params_json=json.dumps(
                {"entry_windows": report.manifest.get("entry_windows"),
                 "test_blocks": report.manifest.get("test_blocks"),
                 "holdout_ratio": report.manifest.get("holdout_ratio")},
                ensure_ascii=False),
            profile_source="futures_cli",
            report_json=json.dumps({"manifest": report.manifest, "notes": notes},
                                   ensure_ascii=False, default=str),
        )
    return report, notes


def _auto_run_id(product: str, prefix: str = "fbt") -> str:
    return f"{prefix}-{product.lower()}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
