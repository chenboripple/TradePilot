import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ripple_tradePilot import __version__
from ripple_tradePilot.api.dashboard import DashboardDataError, DashboardService
from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import compute_metrics, compute_trade_stats
from ripple_tradePilot.models.types import Bar
from ripple_tradePilot.strategies.bollinger import BollingerBands
from ripple_tradePilot.strategies.donchian import DonchianBreakout
from ripple_tradePilot.strategies.macd import MACD
from ripple_tradePilot.strategies.moving_average import MovingAverageCross
from ripple_tradePilot.strategies.rsi import RSI
from ripple_tradePilot.data.stock_service import (
    InvalidStockSymbolError,
    StockDataService,
    StockDataUnavailableError,
)
from ripple_tradePilot.storage.database import (
    init_database,
    list_stock_catalog,
    load_daily_bars,
    load_stock_quotes,
    stock_catalog_name,
    stock_catalog_names,
)
from ripple_tradePilot.storage.user_store import (
    SESSION_DAYS,
    StrategyNotFoundError,
    UsernameTakenError,
    WatchlistExistsError,
    WatchlistNotFoundError,
    authenticate_user,
    create_session,
    create_strategy,
    create_user,
    delete_session,
    ensure_system_strategies,
    list_user_backtests,
    list_user_stocks,
    list_visible_strategies,
    list_user_watchlist,
    set_watchlist_default_strategy,
    upsert_watchlist_item,
    update_strategy,
    update_strategy_visibility,
    user_for_session,
)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "tradepilot_session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    _sync_configured_system_strategies()
    yield


app = FastAPI(title="TradePilot API", version=__version__, lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    asset_class: Literal["stock", "future"]
    symbol: str = Field(min_length=1, max_length=32)
    profile: str = Field(min_length=1, max_length=80)
    parameters: Dict[str, float]
    visibility: Literal["public", "private"] = "private"


class StrategyVisibilityUpdate(BaseModel):
    visibility: Literal["public", "private"]


class StrategyUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    profile: str = Field(min_length=1, max_length=80)
    parameters: Dict[str, float]
    visibility: Literal["public", "private"]


class DefaultStrategyUpdate(BaseModel):
    strategy_id: Optional[int] = Field(default=None, ge=1)


class WatchlistCreate(BaseModel):
    symbol: str = Field(min_length=6, max_length=16)


def _normalize_username(username: str) -> str:
    normalized = username.strip()
    if "@" in normalized:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise HTTPException(status_code=422, detail="邮箱格式不正确")
        return normalized.lower()
    if len(normalized) > 32:
        raise HTTPException(status_code=422, detail="用户名不能超过 32 个字符")
    if not all(character.isalnum() or character in "_.-" for character in normalized):
        raise HTTPException(
            status_code=422,
            detail="用户名只能包含字母、数字、点、下划线和连字符，或使用邮箱",
        )
    return normalized


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=os.getenv("TRADEPILOT_SECURE_COOKIE", "false").lower() == "true",
        samesite="lax",
        path="/",
    )


def optional_user(request: Request) -> Optional[Dict]:
    return user_for_session(request.cookies.get(SESSION_COOKIE, ""))


def required_user(user: Optional[Dict] = Depends(optional_user)) -> Dict:
    if user is None:
        raise HTTPException(status_code=401, detail="请先注册或登录")
    return user


def _dashboard_for_user(user: Optional[Dict]) -> DashboardService:
    base_service = DashboardService()
    configured_items = {
        item["code"]: item for item in base_service.configured_assets()
    }
    configured = set(configured_items)
    catalog_names = stock_catalog_names()
    records = list_user_stocks(user["id"]) if user is not None else []
    visible_strategies = (
        {item["id"]: item for item in list_visible_strategies(user["id"])}
        if user is not None
        else {}
    )
    symbols = []
    for code, item in configured_items.items():
        if code in catalog_names:
            symbols.append({**item, "name": catalog_names[code]})
    for item in records:
        if not item["is_watched"]:
            continue
        is_user_added = item["symbol"] not in configured
        symbol = {
            "code": item["symbol"],
            "name": catalog_names.get(item["symbol"], item["name"]),
            "asset_class": "stock",
            "user_added": is_user_added,
        }
        if is_user_added:
            symbol["strategy_profile"] = "默认组合策略"
        default_strategy = visible_strategies.get(item.get("default_strategy_id"))
        if (
            default_strategy is not None
            and default_strategy["asset_class"] == "stock"
            and default_strategy["symbol"] == item["symbol"]
        ):
            symbol.update(
                {
                    "default_strategy_id": default_strategy["id"],
                    "default_strategy_name": default_strategy["name"],
                    "default_strategy_parameters": default_strategy["parameters"],
                }
            )
        symbols.append(symbol)
    excluded = [item["symbol"] for item in records if not item["is_watched"]]
    return DashboardService(extra_symbols=symbols, excluded_symbols=excluded)


def _sync_configured_system_strategies():
    templates = DashboardService().strategy_catalog()
    strategies_by_owner: Dict[str, list] = {}
    for item in templates:
        strategies_by_owner.setdefault(item["owner"], []).append(item)
    bound_keys = set()
    for owner, items in strategies_by_owner.items():
        bound_keys.update(ensure_system_strategies(owner, items))
    return templates, bound_keys


def _configured_stock_map() -> Dict[str, Dict]:
    return {
        item["code"]: item
        for item in DashboardService().configured_assets()
        if item.get("asset_class") == "stock"
    }


def _stock_records(user: Optional[Dict]) -> Dict[str, Dict]:
    if user is None:
        return {}
    return {item["symbol"]: item for item in list_user_stocks(user["id"])}


def _stock_catalog(user: Optional[Dict]) -> Dict:
    configured = _configured_stock_map()
    records = _stock_records(user)
    symbols: Dict[str, Dict] = {
        item["symbol"]: dict(item) for item in list_stock_catalog()
    }
    empty_market = {
        "market": "",
        "exchange": "",
        "board": "",
        "industry": "",
        "area": "",
        "list_status": "L",
        "list_date": "",
        "updated_at": None,
        "latest_date": None,
        "price": None,
        "change": None,
        "change_pct": None,
        "pre_close": None,
        "price_time": None,
        "price_source": None,
        "price_kind": "unavailable",
        "quote_time": None,
        "quote_volume": None,
        "quote_amount": None,
        "turnover_rate": None,
    }
    for code, item in configured.items():
        symbols.setdefault(
            code,
            {
                **empty_market,
                "symbol": code,
                "name": item.get("name", code),
                "source": "config",
            },
        )
    for code, item in records.items():
        symbols.setdefault(
            code,
            {
                **empty_market,
                "symbol": code,
                "name": item["name"],
                "source": "watchlist",
            },
        )

    items = []
    for code in sorted(symbols):
        symbol = symbols[code]
        record = records.get(code)
        watched = record["is_watched"] if record is not None else code in configured
        latest_date = symbol.get("latest_date")
        price_time = symbol.get("price_time") or latest_date
        freshness = "unavailable"
        if price_time:
            try:
                parsed = datetime.fromisoformat(str(price_time).replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.strptime(str(price_time)[:8], "%Y%m%d")
            freshness = (
                "fresh"
                if max((datetime.now().date() - parsed.date()).days, 0) <= 4
                else "stale"
            )
        item = {
            "symbol": code,
            "name": symbol.get("name", code),
            "market": symbol.get("market", ""),
            "exchange": symbol.get("exchange", ""),
            "board": symbol.get("board") or symbol.get("market", ""),
            "industry": symbol.get("industry", ""),
            "area": symbol.get("area", ""),
            "list_status": symbol.get("list_status", "L"),
            "list_date": symbol.get("list_date", ""),
            "catalog_updated_at": symbol.get("updated_at"),
            "is_watched": watched,
            "is_default": code in configured,
            "last_updated_at": record.get("last_updated_at") if record else None,
            "price": symbol.get("price"),
            "pre_close": symbol.get("pre_close"),
            "change": symbol.get("change"),
            "change_pct": symbol.get("change_pct"),
            "latest_date": latest_date,
            "price_time": price_time,
            "price_source": symbol.get("price_source"),
            "price_kind": symbol.get("price_kind", "unavailable"),
            "quote_time": symbol.get("quote_time"),
            "volume": symbol.get("quote_volume"),
            "amount": symbol.get("quote_amount"),
            "turnover_rate": symbol.get("turnover_rate"),
            "freshness": freshness,
        }
        items.append(item)
    return {"items": items}


def _stock_error(error: RuntimeError) -> HTTPException:
    status_code = 422 if isinstance(error, InvalidStockSymbolError) else 503
    return HTTPException(status_code=status_code, detail=str(error))


@app.get("/", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(credentials: Credentials, response: Response):
    username = _normalize_username(credentials.username)
    try:
        user = create_user(username, credentials.password)
    except UsernameTakenError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _sync_configured_system_strategies()
    _set_session_cookie(response, create_session(user["id"]))
    return {"user": user}


@app.post("/api/auth/login")
def login(credentials: Credentials, response: Response):
    username = _normalize_username(credentials.username)
    user = authenticate_user(username, credentials.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _set_session_cookie(response, create_session(user["id"]))
    return {"user": user}


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response):
    delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")


@app.get("/api/auth/me")
def auth_me(user: Optional[Dict] = Depends(optional_user)):
    return {"user": user}


@app.get("/api/dashboard")
def dashboard(user: Optional[Dict] = Depends(optional_user)):
    return _dashboard_for_user(user).dashboard()


@app.get("/api/markets/{symbol}")
def market_detail(
    symbol: str,
    limit: int = Query(default=160, ge=40, le=260),
    strategy_id: Optional[int] = Query(default=None, ge=1),
    system_strategy: bool = Query(default=False),
    user: Optional[Dict] = Depends(optional_user),
):
    try:
        strategy = None
        if strategy_id is not None:
            if user is None:
                raise HTTPException(status_code=401, detail="请先注册或登录")
            strategy = next(
                (
                    item
                    for item in list_visible_strategies(user["id"])
                    if item["id"] == strategy_id and item["symbol"] == symbol
                ),
                None,
            )
            if strategy is None:
                raise HTTPException(status_code=404, detail="策略不存在、不可见或不适用于当前标的")

        detail = _dashboard_for_user(user).market_detail(
            symbol,
            limit=limit,
            profile_override=strategy["parameters"] if strategy else None,
            strategy_profile=strategy["name"] if strategy else None,
            use_default_strategy=not system_strategy,
        )
        if strategy is not None and strategy["asset_class"] != detail["asset_class"]:
            raise HTTPException(status_code=404, detail="策略不存在、不可见或不适用于当前标的")
        return detail
    except DashboardDataError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/strategies")
def strategies(user: Dict = Depends(required_user)):
    system_strategies, bound_keys = _sync_configured_system_strategies()
    user_strategies = list_visible_strategies(user["id"])
    unbound_system_strategies = [
        item for item in system_strategies if item["system_key"] not in bound_keys
    ]
    return {"items": user_strategies + unbound_system_strategies}


@app.post("/api/strategies", status_code=status.HTTP_201_CREATED)
def add_strategy(payload: StrategyCreate, user: Dict = Depends(required_user)):
    strategy = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    strategy["name"] = strategy["name"].strip()
    if strategy["asset_class"] == "stock":
        try:
            strategy["symbol"] = StockDataService.normalize_symbol(strategy["symbol"])
        except InvalidStockSymbolError as error:
            raise _stock_error(error) from error
        if (
            not stock_catalog_name(strategy["symbol"])
            and strategy["symbol"] not in _configured_stock_map()
        ):
            raise HTTPException(status_code=422, detail="股票标的必须来自全部数据池")
    else:
        strategy["symbol"] = strategy["symbol"].strip().upper()
    strategy["profile"] = strategy["profile"].strip()
    return {"item": create_strategy(user["id"], strategy)}


@app.patch("/api/strategies/{strategy_id}/visibility")
def change_strategy_visibility(
    strategy_id: int,
    payload: StrategyVisibilityUpdate,
    user: Dict = Depends(required_user),
):
    try:
        item = update_strategy_visibility(strategy_id, user["id"], payload.visibility)
    except StrategyNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"item": item}


@app.patch("/api/strategies/{strategy_id}")
def change_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    user: Dict = Depends(required_user),
):
    strategy = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    strategy["name"] = strategy["name"].strip()
    strategy["profile"] = strategy["profile"].strip()
    try:
        item = update_strategy(strategy_id, user["id"], strategy)
    except StrategyNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"item": item}


@app.get("/api/backtests")
def backtests(user: Dict = Depends(required_user)):
    futures_suffixes = (".CFFEX", ".SHFE", ".DCE", ".CZCE", ".INE", ".GFEX")
    items = [
        {
            **result,
            "asset_class": (
                "future"
                if str(result.get("symbol", "")).endswith(futures_suffixes)
                else "stock"
            ),
        }
        for result in list_user_backtests(user["id"])
    ]
    return {"items": items}


@app.get("/api/watchlist")
def watchlist(user: Dict = Depends(required_user)):
    return {"items": list_user_watchlist(user["id"])}


@app.patch("/api/watchlist/{symbol}/default-strategy")
def change_watchlist_default_strategy(
    symbol: str,
    payload: DefaultStrategyUpdate,
    user: Dict = Depends(required_user),
):
    try:
        normalized = StockDataService.normalize_symbol(symbol)
    except InvalidStockSymbolError as error:
        raise _stock_error(error) from error

    configured = _configured_stock_map()
    records = _stock_records(user)
    record = records.get(normalized)
    is_watched = (
        record["is_watched"] if record is not None else normalized in configured
    )
    if not is_watched:
        raise HTTPException(status_code=404, detail="只能设置当前观察池股票的默认策略")

    if payload.strategy_id is not None:
        strategy = next(
            (
                item
                for item in list_visible_strategies(user["id"])
                if item["id"] == payload.strategy_id
                and item["asset_class"] == "stock"
                and item["symbol"] == normalized
            ),
            None,
        )
        if strategy is None:
            raise HTTPException(
                status_code=404,
                detail="策略不存在、不可见或不适用于当前标的",
            )

    if record is None:
        configured_item = configured[normalized]
        upsert_watchlist_item(
            user["id"],
            normalized,
            configured_item.get("name", normalized),
            True,
        )
    try:
        item = set_watchlist_default_strategy(
            user["id"], normalized, payload.strategy_id
        )
    except WatchlistNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"item": item}


@app.get("/api/stocks")
def stocks(user: Optional[Dict] = Depends(optional_user)):
    return _stock_catalog(user)


@app.post("/api/stocks/refresh")
def refresh_stocks(user: Dict = Depends(required_user)):
    try:
        return {"data": StockDataService().refresh_catalog()}
    except StockDataUnavailableError as error:
        raise _stock_error(error) from error


@app.post("/api/stocks/quotes/refresh")
def refresh_stock_quotes(user: Dict = Depends(required_user)):
    try:
        return {"data": StockDataService().refresh_quotes()}
    except StockDataUnavailableError as error:
        raise _stock_error(error) from error


# 市场总览：本地快照超过该时长视为过期，先尝试刷新一次
MARKET_OVERVIEW_MAX_AGE = timedelta(minutes=5)
# 涨跌停近似阈值（主板 ±10%；创业板/科创板 ±20%，此处统一近似，见 breadth 注释）
MARKET_LIMIT_PCT = 9.8


def _parse_quote_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _latest_quote_time(rows) -> tuple:
    """返回 (最新 quote_time 的 datetime, 原始字符串)；无有效值返回 (None, "")。"""
    latest: Optional[datetime] = None
    raw = ""
    for row in rows:
        parsed = _parse_quote_time(row.get("quote_time"))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
            raw = str(row.get("quote_time"))
    return latest, raw


def _market_overview_data() -> Dict[str, Any]:
    service = StockDataService()
    rows = load_stock_quotes()
    latest, quote_time = _latest_quote_time(rows)
    # 快照为空或已过期：先刷新一次再重算；刷新失败时沿用旧快照并标记 stale
    if not rows or latest is None or datetime.now() - latest > MARKET_OVERVIEW_MAX_AGE:
        try:
            service.refresh_quotes()
        except StockDataUnavailableError:
            pass
        rows = load_stock_quotes()
        latest, quote_time = _latest_quote_time(rows)
    if not rows:
        raise StockDataUnavailableError("暂无全市场行情快照，请稍后重试")
    stale = latest is None or datetime.now() - latest > MARKET_OVERVIEW_MAX_AGE

    breadth = {"total": len(rows), "up": 0, "flat": 0, "down": 0, "limit_up": 0, "limit_down": 0}
    turnover = 0.0
    for row in rows:
        change_pct = row.get("change_pct")
        if change_pct is None:
            breadth["flat"] += 1
        elif change_pct > 0:
            breadth["up"] += 1
        elif change_pct < 0:
            breadth["down"] += 1
        else:
            breadth["flat"] += 1
        if change_pct is not None:
            # 近似口径：主板涨跌停为 ±10%，创业板/科创板为 ±20%，此处统一按 ±9.8% 估算
            if change_pct >= MARKET_LIMIT_PCT:
                breadth["limit_up"] += 1
            elif change_pct <= -MARKET_LIMIT_PCT:
                breadth["limit_down"] += 1
        amount = row.get("amount")
        if amount is not None:
            turnover += float(amount)

    # 市场宽度优先用妙想（结构不保证，解析失败回退本地快照统计）
    breadth_from_mx = False
    try:
        mx_breadth = service.fetch_market_breadth_mx()
    except Exception:
        mx_breadth = None
    if mx_breadth:
        breadth["up"] = mx_breadth["up"]
        breadth["down"] = mx_breadth["down"]
        breadth["flat"] = mx_breadth["flat"]
        breadth["total"] = mx_breadth["up"] + mx_breadth["down"] + mx_breadth["flat"]
        breadth_from_mx = True

    total = breadth["total"]
    up_ratio = round(breadth["up"] / total, 2) if total else 0.0
    if up_ratio >= 0.6:
        label = "偏强"
    elif up_ratio <= 0.4:
        label = "偏弱"
    else:
        label = "均衡"

    indices_payload = service.fetch_index_quotes()
    indices = indices_payload.get("indices", [])
    index_source = indices_payload.get("source", "")
    index_label = {"mx": "mx", "sina": "sina", "akshare": "akshare"}.get(index_source, "")
    breadth_label = "mx" if breadth_from_mx else "snapshot"
    source_parts = []
    for part in (index_label, breadth_label):
        if part and part not in source_parts:
            source_parts.append(part)
    source = "+".join(source_parts) if source_parts else "snapshot"

    return {
        "quote_time": quote_time,
        "indices": indices,
        "breadth": breadth,
        "turnover": turnover,
        "sentiment": {"up_ratio": up_ratio, "label": label},
        "stale": stale,
        "source": source,
    }


@app.get("/api/market/overview")
def market_overview(user: Dict = Depends(required_user)):
    try:
        return {"data": _market_overview_data()}
    except StockDataUnavailableError as error:
        raise _stock_error(error) from error


class BacktestRequest(BaseModel):
    symbol: str
    strategy: Literal["ma", "rsi", "macd", "bollinger", "donchian"] = "rsi"
    bars: int = Field(252, ge=60, le=2500)
    cash: float = Field(100000.0, gt=0)
    execution: Literal["next_open", "close"] = "next_open"


_BACKTEST_STRATEGIES = {
    "ma": MovingAverageCross,
    "rsi": RSI,
    "macd": MACD,
    "bollinger": BollingerBands,
    "donchian": DonchianBreakout,
}


@app.post("/api/backtest")
def run_web_backtest(payload: BacktestRequest, user: Dict = Depends(required_user)):
    """统一引擎回测：次日开盘撮合、涨跌停拦截、100 股整数倍、佣金+印花税+滑点。"""
    try:
        symbol = StockDataService.normalize_symbol(payload.symbol)
        rows = load_daily_bars(symbol)
        if len(rows) < payload.bars:
            # 本地日线不足时先刷新行情再回测
            StockDataService().refresh(symbol, initial_days=max(payload.bars + 60, 365))
            rows = load_daily_bars(symbol)
        if len(rows) < 60:
            raise StockDataUnavailableError(f"{symbol} 的日线数据不足，无法回测")

        rows = rows[-payload.bars:]
        bars = []
        for row in rows:
            open_price, high = float(row["open"]), float(row["high"])
            low, close = float(row["low"]), float(row["close"])
            if min(open_price, high, low, close) <= 0 or high < low:
                continue
            bars.append(
                Bar(
                    timestamp=datetime.strptime(row["trade_date"], "%Y%m%d"),
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=float(row.get("vol") or 0),
                )
            )

        result = run_backtest(
            strategy=_BACKTEST_STRATEGIES[payload.strategy](),
            bars=bars,
            initial_cash=payload.cash,
            execution=payload.execution,
        )
        metrics = compute_metrics(result.equity_curve)
        stats = compute_trade_stats(result.fills)
        return {
            "data": {
                "symbol": symbol,
                "strategy": payload.strategy,
                "execution": payload.execution,
                "bar_count": len(bars),
                "metrics": {
                    "total_return": metrics.total_return,
                    "annual_return": metrics.annual_return,
                    "max_drawdown": metrics.max_drawdown,
                    "sharpe": metrics.sharpe,
                },
                "trades": {
                    "num_trades": stats.num_trades,
                    "win_rate": stats.win_rate,
                    "avg_return_per_trade": stats.avg_return_per_trade,
                    "best_trade": stats.best_trade,
                    "worst_trade": stats.worst_trade,
                    "total_fees": stats.total_fees,
                },
                "halted_by_drawdown": result.halted_by_drawdown,
                "skipped_fills": len(result.skipped_fills),
                "equity_curve": [
                    {"date": bars[index].timestamp.strftime("%Y-%m-%d"), "equity": value}
                    for index, value in enumerate(result.equity_curve)
                    if index < len(bars)
                ],
                "fills": [
                    {
                        "date": fill.timestamp.strftime("%Y-%m-%d"),
                        "side": fill.side.value,
                        "quantity": fill.quantity,
                        "price": fill.price,
                        "fee": fill.fee,
                    }
                    for fill in result.fills
                ],
                "disclaimer": "样本内回测仅供参考，未经样本外验证的收益不可作为预期收益。",
            }
        }
    except (InvalidStockSymbolError, StockDataUnavailableError) as error:
        raise _stock_error(error) from error


@app.post("/api/watchlist", status_code=status.HTTP_201_CREATED)
def add_to_watchlist(payload: WatchlistCreate, user: Dict = Depends(required_user)):
    service = StockDataService()
    try:
        symbol = service.normalize_symbol(payload.symbol)
        configured = _configured_stock_map()
        records = _stock_records(user)
        record = records.get(symbol)
        if (record and record["is_watched"]) or (record is None and symbol in configured):
            raise WatchlistExistsError("该股票已在观察池中")
        if record is not None or symbol in configured:
            name = record["name"] if record else configured[symbol].get("name", symbol)
            item = upsert_watchlist_item(user["id"], symbol, name, True)
            refreshed = None
        else:
            refreshed = service.refresh(symbol, initial_days=365)
            if refreshed["name"] == refreshed["symbol"]:
                raise StockDataUnavailableError(
                    f"已获取 {symbol} 的行情，但暂时无法解析股票名称，请稍后重试"
                )
            item = upsert_watchlist_item(
                user["id"],
                refreshed["symbol"],
                refreshed["name"],
                True,
                mark_updated=True,
            )
    except WatchlistExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (InvalidStockSymbolError, StockDataUnavailableError) as error:
        raise _stock_error(error) from error
    return {"item": item, "data": refreshed}


@app.post("/api/watchlist/{symbol}/refresh")
def refresh_watchlist_stock(symbol: str, user: Dict = Depends(required_user)):
    service = StockDataService()
    try:
        normalized = service.normalize_symbol(symbol)
        records = _stock_records(user)
        record = records.get(normalized)
        is_watched = (
            record["is_watched"]
            if record is not None
            else normalized in _configured_stock_map()
        )
        if not is_watched:
            raise WatchlistNotFoundError("只能更新当前观察池中的股票")
        refreshed = service.refresh(normalized, initial_days=365)
        refreshed_name = (
            record["name"]
            if record is not None
            else _configured_stock_map()[normalized].get("name", normalized)
        )
        upsert_watchlist_item(
            user["id"],
            normalized,
            refreshed_name,
            True,
            mark_updated=True,
        )
    except WatchlistNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (InvalidStockSymbolError, StockDataUnavailableError) as error:
        raise _stock_error(error) from error
    return {"data": refreshed}


@app.delete("/api/watchlist/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_watchlist(symbol: str, user: Dict = Depends(required_user)):
    try:
        normalized = StockDataService.normalize_symbol(symbol)
        configured = _configured_stock_map()
        records = _stock_records(user)
        record = records.get(normalized)
        is_watched = (
            record["is_watched"] if record is not None else normalized in configured
        )
        if not is_watched:
            raise WatchlistNotFoundError("观察池中不存在该股票")
        name = (
            record["name"]
            if record is not None
            else configured[normalized].get("name", normalized)
        )
        upsert_watchlist_item(user["id"], normalized, name, False)
    except InvalidStockSymbolError as error:
        raise _stock_error(error) from error
    except WatchlistNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
