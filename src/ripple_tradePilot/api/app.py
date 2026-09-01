import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ripple_tradePilot.api.dashboard import DashboardDataError, DashboardService
from ripple_tradePilot.data.stock_service import (
    InvalidStockSymbolError,
    StockDataService,
    StockDataUnavailableError,
)
from ripple_tradePilot.storage.database import (
    init_database,
    list_stock_catalog,
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
    list_user_backtests,
    list_user_stocks,
    list_visible_strategies,
    list_user_watchlist,
    upsert_watchlist_item,
    update_strategy_visibility,
    user_for_session,
)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "tradepilot_session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    yield


app = FastAPI(title="TradePilot API", version="0.4.0", lifespan=lifespan)
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
        symbols.append(symbol)
    excluded = [item["symbol"] for item in records if not item["is_watched"]]
    return DashboardService(extra_symbols=symbols, excluded_symbols=excluded)


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
        "list_status": "L",
        "list_date": "",
        "updated_at": None,
        "latest_date": None,
        "price": None,
        "change": None,
        "change_pct": None,
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
        freshness = "unavailable"
        if latest_date:
            try:
                parsed = datetime.strptime(str(latest_date)[:10], "%Y-%m-%d")
            except ValueError:
                parsed = datetime.strptime(str(latest_date)[:8], "%Y%m%d")
            freshness = (
                "fresh"
                if max((datetime.now().date() - parsed.date()).days, 0) <= 4
                else "stale"
            )
        item = {
            "symbol": code,
            "name": symbol.get("name", code),
            "market": symbol.get("market", ""),
            "list_status": symbol.get("list_status", "L"),
            "list_date": symbol.get("list_date", ""),
            "catalog_updated_at": symbol.get("updated_at"),
            "is_watched": watched,
            "is_default": code in configured,
            "last_updated_at": record.get("last_updated_at") if record else None,
            "price": symbol.get("price"),
            "change": symbol.get("change"),
            "change_pct": symbol.get("change_pct"),
            "latest_date": latest_date,
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
    user: Optional[Dict] = Depends(optional_user),
):
    try:
        return _dashboard_for_user(user).market_detail(symbol, limit=limit)
    except DashboardDataError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/strategies")
def strategies(user: Dict = Depends(required_user)):
    system_strategies = DashboardService().strategy_catalog()
    user_strategies = list_visible_strategies(user["id"])
    return {"items": user_strategies + system_strategies}


@app.post("/api/strategies", status_code=status.HTTP_201_CREATED)
def add_strategy(payload: StrategyCreate, user: Dict = Depends(required_user)):
    strategy = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    strategy["name"] = strategy["name"].strip()
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


@app.get("/api/stocks")
def stocks(user: Optional[Dict] = Depends(optional_user)):
    return _stock_catalog(user)


@app.post("/api/stocks/refresh")
def refresh_stocks(user: Dict = Depends(required_user)):
    try:
        return {"data": StockDataService().refresh_catalog()}
    except StockDataUnavailableError as error:
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
