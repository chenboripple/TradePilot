from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ripple_tradePilot.api.dashboard import DashboardDataError, DashboardService

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TradePilot API", version="0.2.0")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard():
    return DashboardService().dashboard()


@app.get("/api/markets/{symbol}")
def market_detail(symbol: str, limit: int = Query(default=160, ge=40, le=260)):
    try:
        return DashboardService().market_detail(symbol, limit=limit)
    except DashboardDataError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
