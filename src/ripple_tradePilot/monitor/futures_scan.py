"""P1 期货观察池扫描编排：sync → 主力 → 倾向 → 风险 → 持久化去重通知。

一条链路串起 roadmap P1 的全部验收点：

1. **合约发现与快照**：``futures_comm_info`` 一次拿到全部在市合约的现价/
   涨跌停/每手保证金（P0 §3）；非首期品种整行过滤。
2. **日线同步**：每合约全量拉新浪日线（接口无起止参数、60m 仅 ~8.5 个月，
   全量+幂等 upsert 成本最低）；量能/持仓判断只用日线（P0 §4.3 口径）。
3. **主力判定**：最新**已收盘共同**交易日 volume 最大者（P0 §5，无前视），
   覆盖写回 futures_quotes.is_main——comm_info「备注」的主力标记只是源方
   口径，不作为本系统主力的真源。
4. **60m 同步 + 倾向**：只为主力合约同步 60m（控制调用量），**只喂已完成
   bar**（bar_time 为结束时刻；标签在未来 = 未收线，过滤掉）→ Donchian
   倾向（signals.futures_eval，纯函数）。
5. **风险测算**：§5 口径（risk.sizing），三情形 gate 不过 → 不可执行。
6. **通知**：notify_log 持久化去重（先记录后发送，重启不重发——roadmap
   P1 原文），outbox 返回给调用方（CLI/monitor）负责实际投递。

网络全部收敛在本模块（被 CLI ``tradepilot futures scan --once`` 与后续
monitor 循环调用）；API 端点只读 DB。到期日 P1 用「到期月首日」近似锚
（保守：真实到期在月中，提醒只会更早不会更晚），SHFE 精确到期日接口
留 P2。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ripple_tradePilot.config_loader import load_config
from ripple_tradePilot.data.futures_calendar import TradingCalendar
from ripple_tradePilot.data.futures_meta import (
    PRODUCT_SPECS,
    parse_contract_symbol,
)
from ripple_tradePilot.data.futures_service import (
    TIMEFRAME_60M,
    TIMEFRAME_DAILY,
    FuturesDataError,
    FuturesDataService,
)
from ripple_tradePilot.risk.sizing import RiskReport, SizingParams, risk_report
from ripple_tradePilot.signals.futures_eval import (
    DonchianParams,
    FuturesTilt,
    TILT_NEUTRAL,
    evaluate_tilt,
)
from ripple_tradePilot.storage.database import (
    load_futures_bars,
    load_futures_quote,
    load_futures_quotes,
    record_notification,
    upsert_futures_bars,
    upsert_futures_contracts,
    upsert_futures_quotes,
)


def _identity(symbol: str):
    """canonical（RB2610.SHFE）或裸代码 → ContractIdentity（同 service 口径）。"""
    return FuturesDataService._identity(symbol)

#: 近月到期提示窗口（日历日）：距到期月首日不足该天数 → 提醒（近似口径偏保守）
NEAR_EXPIRY_BUFFER_DAYS = 30

#: 倾向评估喂入的 60m bar 数（entry 20 + ATR 14 远够，多留上下文无成本）
_TILT_BAR_CONTEXT = 100


class _DbStore:
    """futures_service._Store 协议 → storage.database 访问器的适配器。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = path

    def latest_bar(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        from ripple_tradePilot.storage.database import latest_futures_bar

        return latest_futures_bar(symbol, timeframe, self.path)

    def upsert_bars(self, timeframe: str, rows: List[Dict[str, Any]]) -> int:
        return upsert_futures_bars(timeframe, rows, self.path)


@dataclass
class ProductScan:
    """单品种一次扫描的结论（含失败原因——单品种失败不拖垮整轮）。"""

    product: str
    name: str
    main_symbol: Optional[str] = None       # 本轮判定的主力（None=判定失败）
    contracts_synced: int = 0
    tilt: Optional[FuturesTilt] = None      # None=没法算（数据不足），非 None 含 NEUTRAL
    risk: Optional[RiskReport] = None
    roll_alert: bool = False                # 主力发生切换
    near_expiry: bool = False
    errors: List[str] = field(default_factory=list)


@dataclass
class ScanReport:
    """整轮扫描汇报：products 逐品种 + errors 全局错误 + outbox 待发通知。

    outbox 是「本轮新出现的去重键」（record_notification 首次 True）对应的消息，
    调用方负责投递；投递失败不回滚记录（去重优先于补发，roadmap P1 口径）。
    """

    started_at: str
    products: List[ProductScan] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    outbox: List[Dict[str, str]] = field(default_factory=list)  # {kind,dedup_key,message}

    @property
    def ok(self) -> bool:
        return not self.errors and all(not item.errors for item in self.products)


def _notify(
    report: ScanReport,
    dedup_key: str,
    kind: str,
    message: str,
    path: Optional[Path] = None,
    payload_json: str = "",
) -> None:
    """记录去重键；首次出现才进 outbox（重启后同键 False → 不重发）。"""
    if record_notification(
        dedup_key, "feishu", kind, payload_json=payload_json, path=path
    ):
        report.outbox.append(
            {"kind": kind, "dedup_key": dedup_key, "message": message}
        )


def _previous_main(product: str, path: Optional[Path]) -> Optional[str]:
    """库内上一轮的主力（本轮 upsert 前读取，判定换月用）。"""
    for row in load_futures_quotes(path):
        if not row.get("is_main"):
            continue
        try:
            identity = _identity(str(row.get("symbol") or ""))
        except ValueError:
            continue  # 库里若残留范围外合约（历史遗留），不当崩
        if identity.product == product:
            return identity.canonical
    return None


def _completed_bars(
    symbol: str, timeframe: str, now: datetime, path: Optional[Path]
) -> List[Dict[str, Any]]:
    """已完成 bar：bar_time 是结束时刻，标签晚于 now = 未收线，剔除。"""
    bars = load_futures_bars(symbol, timeframe, path=path)
    completed = []
    for bar in bars:
        text = str(bar.get("bar_time") or "")
        try:
            bar_dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if bar_dt <= now:
            completed.append(bar)
    return completed[-_TILT_BAR_CONTEXT:]


def _risk_params(config: Dict[str, Any]) -> SizingParams:
    """config.futures_risk → SizingParams（§5：预算=资金×比例；缺省兜底并注明）。"""
    risk_config = dict(config.get("futures_risk") or {})
    capital = float(risk_config.get("capital") or 100000)
    budget_pct = float(risk_config.get("risk_budget_pct") or 0.01)
    return SizingParams(risk_budget=capital * budget_pct, available_cash=capital)


def scan_once(
    config: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    products: Optional[List[str]] = None,
) -> ScanReport:
    """跑一轮 P1 观察池扫描（全网络在此，纯函数层全部离线可测）。

    单品种失败记入该品种 errors 并继续（一个品种源故障不拖垮整轮）；
    全局失败（comm_info 拉不到 → 无合约可发现）记入 report.errors。
    """
    config = config or load_config()
    now = now or datetime.now()
    report = ScanReport(started_at=now.strftime("%Y-%m-%d %H:%M:%S"))
    store = _DbStore(path)
    service = FuturesDataService(store=store)
    donchian = DonchianParams()
    sizing = _risk_params(config)

    # 1. 合约快照（发现 + 现价/保证金）；全局失败 → 无从扫描，如实返回
    try:
        quote_rows = service.refresh_quote_snapshot()
    except FuturesDataError as error:
        report.errors.append(f"合约快照获取失败：{error}")
        return report
    if not quote_rows:
        report.errors.append("合约快照无首期品种数据（源异常或品种全部未挂牌）")
        return report

    by_product: Dict[str, List[Dict[str, Any]]] = {}
    for row in quote_rows:
        by_product.setdefault(_identity(row["symbol"]).product, []).append(row)
    quote_by_symbol = {row["symbol"]: row for row in quote_rows}

    # 2. 日线全量同步（合约发现自 comm_info；trade_date 归属在 normalize 内完成）
    daily_rows_by_product: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    calendar_dates = set()
    for product in sorted(products or PRODUCT_SPECS):
        scan = ProductScan(product=product, name=PRODUCT_SPECS[product].name)
        report.products.append(scan)
        rows = by_product.get(product) or []
        if not rows:
            scan.errors.append("快照中无该品种合约")
            continue
        for quote in rows:
            try:
                # 日线归一化本身不需要日历（无夜盘归属问题），传空日历只为满足
                # 签名；本轮不消费该报告的 gaps/freshness（首扫尚无日历可依，
                # 空日历会把这些结论标为近似——如实弃用而非误读）
                sync = service.sync_contract(
                    quote["symbol"], TIMEFRAME_DAILY, TradingCalendar([]), now=now
                )
                scan.contracts_synced += 1
                bars = load_futures_bars(sync.symbol, TIMEFRAME_DAILY, path=path)
                daily_rows_by_product.setdefault(product, {})[sync.symbol] = bars
                for bar in bars:
                    text = str(bar.get("trade_date") or "")
                    if len(text) == 8:
                        calendar_dates.add(
                            date(int(text[:4]), int(text[4:6]), int(text[6:8]))
                        )
            except FuturesDataError as error:
                scan.errors.append(str(error))
    calendar = TradingCalendar(calendar_dates)

    # 3. 主力判定 + 快照/合约元数据落库（换月提醒 = 与库内上一轮主力比对）
    for scan in report.products:
        product = scan.product
        spec = PRODUCT_SPECS[product]
        series = daily_rows_by_product.get(product) or {}
        contract_rows = []
        for quote in by_product.get(product) or []:
            identity = _identity(quote["symbol"])
            contract_rows.append(
                {
                    "symbol": identity.canonical,
                    "product": product,
                    "exchange": spec.exchange,
                    "name": f"{spec.name}{identity.year % 100:02d}{identity.month:02d}",
                    "multiplier": spec.multiplier,
                    "tick_size": spec.tick_size,
                    "night_start": spec.night_start,
                    "night_end": spec.night_end,
                    # P1 近似锚=到期月首日（保守）；SHFE 精确到期日接口留 P2
                    "listed_date": "",
                    "expiry_date": f"{identity.year}{identity.month:02d}01",
                    "expiry_is_approximate": True,
                    "rule_version": spec.rule_version,
                }
            )
        if contract_rows:
            upsert_futures_contracts(contract_rows, path)

        try:
            main_symbol = service.resolve_main(series)
        except FuturesDataError as error:
            scan.errors.append(f"主力判定失败：{error}")
            main_symbol = None
        scan.main_symbol = main_symbol

        previous = _previous_main(product, path)
        if main_symbol and previous and main_symbol != previous:
            scan.roll_alert = True
            identity = _identity(main_symbol)
            _notify(
                report,
                f"{product}|main|{main_symbol}|roll",
                "futures_roll",
                f"🔁 {spec.name}（{product}）主力切换：{previous} → {main_symbol}"
                f"（到期 {identity.year}-{identity.month:02d}），请关注换月移仓",
                path=path,
            )

        # 快照落库：is_main 用本轮 volume 口径覆盖源方「备注」标记
        for quote in by_product.get(product) or []:
            quote["is_main"] = quote["symbol"] == main_symbol
        upsert_futures_quotes(by_product.get(product) or [], path)

    # 4. 主力 60m 同步 → 已完成 bar 倾向 → 风险测算（仅主力，控制调用量）
    for scan in report.products:
        if not scan.main_symbol:
            continue
        product = scan.product
        spec = PRODUCT_SPECS[product]
        try:
            service.sync_contract(
                scan.main_symbol, TIMEFRAME_60M, calendar, now=now
            )
        except FuturesDataError as error:
            scan.errors.append(f"60m 同步失败：{error}")
            continue

        bars = _completed_bars(scan.main_symbol, TIMEFRAME_60M, now, path)
        tilt = evaluate_tilt(bars, donchian, symbol=scan.main_symbol)
        scan.tilt = tilt

        quote = load_futures_quote(scan.main_symbol, path) or quote_by_symbol.get(
            scan.main_symbol
        ) or {}
        price = quote.get("price")
        atr = tilt.atr if tilt is not None else None
        scan.risk = risk_report(
            spec,
            symbol=scan.main_symbol,
            price=price,
            price_time=str(quote.get("price_time") or ""),
            atr=atr,
            params=sizing,
            quote_ok=price is not None and bool(quote.get("price_time")),
            margin_per_hand=quote.get("margin_per_hand"),
        )

        # 信号通知：只发非观望倾向；NEUTRAL 也落 notify_log 保审计可追溯
        if tilt is not None:
            payload_json = (
                f'{{"tilt": "{tilt.tilt}", "close": {tilt.close}, '
                f'"as_of": "{tilt.as_of}", "atr": {tilt.atr:.4f}}}'
            )
            key = f"{scan.main_symbol}|{tilt.timeframe}|{tilt.as_of}|{tilt.strategy_version}|signal"
            if tilt.tilt == TILT_NEUTRAL:
                record_notification(
                    key, "feishu", "futures_signal", payload_json, path=path
                )
            else:
                direction = "做多倾向" if tilt.tilt == "LONG" else "做空倾向"
                risk_text = _risk_brief(scan.risk)
                _notify(
                    report,
                    key,
                    "futures_signal",
                    f"📈 {spec.name} {scan.main_symbol} {direction}\n"
                    f"{tilt.basis}\n{risk_text}\n（P1 倾向信号，非可执行委托）",
                    path=path,
                    payload_json=payload_json,
                )

        # 近月到期提醒（近似锚，一次性）
        identity = _identity(scan.main_symbol)
        if service.near_expiry(identity.year, identity.month, now.date(),
                               buffer_days=NEAR_EXPIRY_BUFFER_DAYS):
            scan.near_expiry = True
            anchor = f"{identity.year}{identity.month:02d}01"
            _notify(
                report,
                f"{scan.main_symbol}|expiry|{anchor}|expiry",
                "futures_expiry",
                f"⏰ {spec.name} 主力 {scan.main_symbol} 已进入到期月前 "
                f"{NEAR_EXPIRY_BUFFER_DAYS} 天窗口（到期月 {identity.year}-"
                f"{identity.month:02d}，锚为近似口径），关注换月与个人仓位",
                path=path,
            )

    return report


def _risk_brief(risk: Optional[RiskReport]) -> str:
    """风险摘要（通知用，人话）：可执行给手数，不可执行给原因。"""
    if risk is None:
        return "风险测算：无数据"
    if risk.executable:
        return (
            f"风险测算：每手风险 {risk.per_hand_risk:.0f} 元、保证金 "
            f"{risk.margin_per_hand:.0f} 元/手{'（近似）' if risk.margin_is_estimate else ''}"
            f"，按预算可开 {risk.lots} 手（价格时点 {risk.price_time}）"
        )
    return "风险测算：不可开仓——" + "；".join(risk.reasons)
