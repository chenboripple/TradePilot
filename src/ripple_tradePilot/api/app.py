import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ripple_tradePilot.api.dashboard import DashboardDataError, DashboardService
from ripple_tradePilot.storage.database import init_database
from ripple_tradePilot.storage.user_store import (
    SESSION_DAYS,
    StrategyNotFoundError,
    UsernameTakenError,
    authenticate_user,
    create_session,
    create_strategy,
    create_user,
    delete_session,
    list_user_backtests,
    list_visible_strategies,
    update_strategy_visibility,
    user_for_session,
)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "tradepilot_session"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    yield


app = FastAPI(title="TradePilot API", version="0.3.0", lifespan=lifespan)
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
def dashboard():
    return DashboardService().dashboard()


@app.get("/api/markets/{symbol}")
def market_detail(symbol: str, limit: int = Query(default=160, ge=40, le=260)):
    try:
        return DashboardService().market_detail(symbol, limit=limit)
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
