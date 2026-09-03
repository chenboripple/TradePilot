"""纸面交易账本：记录 → 查询 → 净持仓汇总。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ripple_tradePilot.execution.executor import paper_trade
from ripple_tradePilot.models.types import Bar, Fill, Side, Signal
from ripple_tradePilot.storage import paper_ledger
from ripple_tradePilot.strategies.base import Strategy


def _fill(side: Side, quantity: int, price: float, when: datetime, fee: float = 5.0) -> Fill:
    return Fill(timestamp=when, side=side, quantity=quantity, price=price, fee=fee)


class _RoundTripStrategy(Strategy):
    """第一根 bar 发买入、第三根 bar 发卖出（次日开盘成交）。"""

    name = "round_trip"

    def __init__(self) -> None:
        self._count = 0

    def on_bar(self, bar: Bar) -> Signal:
        self._count += 1
        if self._count == 1:
            side = Side.BUY
        elif self._count == 3:
            side = Side.SELL
        else:
            side = None
        return Signal(timestamp=bar.timestamp, side=side)


def _bars(days=5) -> list[Bar]:
    return [
        Bar(
            timestamp=datetime(2026, 3, day),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            volume=1_000_000,
        )
        for day in range(2, 2 + days)
    ]


def test_record_run_then_query(tmp_path: Path):
    db = tmp_path / "ledger.db"
    morning = datetime(2026, 1, 5, 9, 30)
    afternoon = datetime(2026, 1, 5, 14, 0)
    fills = [
        _fill(Side.SELL, 100, 11.0, afternoon),  # 故意乱序，验证按时间排序
        _fill(Side.BUY, 200, 10.0, morning),
    ]

    returned = paper_ledger.record_run(
        "run-1", "000001.SZ", "ma_cross", 100000.0, 100500.0, fills, path=db
    )

    assert returned == "run-1"
    runs = paper_ledger.list_runs(path=db)
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-1"
    assert runs[0]["symbol"] == "000001.SZ"
    assert runs[0]["strategy"] == "ma_cross"
    assert runs[0]["started_at"] == morning.isoformat()
    assert runs[0]["initial_cash"] == 100000.0
    assert runs[0]["final_equity"] == 100500.0

    run_fills = paper_ledger.get_run_fills("run-1", path=db)
    assert [fill["side"] for fill in run_fills] == ["BUY", "SELL"]
    assert [fill["quantity"] for fill in run_fills] == [200, 100]
    assert run_fills[0]["price"] == 10.0
    assert run_fills[1]["price"] == 11.0
    assert run_fills[0]["symbol"] == "000001.SZ"
    assert paper_ledger.get_run_fills("missing-run", path=db) == []


def test_positions_net_quantity_and_zero_filter(tmp_path: Path):
    db = tmp_path / "ledger.db"
    day1 = datetime(2026, 2, 2, 9, 30)
    day2 = datetime(2026, 2, 3, 9, 30)

    # 平安银行：买 200 卖 100 → 净持仓 100
    paper_ledger.record_run(
        "run-a",
        "000001.SZ",
        "ma_cross",
        100000.0,
        100500.0,
        [_fill(Side.BUY, 200, 10.0, day1), _fill(Side.SELL, 100, 11.0, day2)],
        path=db,
    )
    # 万科：买 100 卖 100 → 已清零，不应出现在持仓里
    paper_ledger.record_run(
        "run-b",
        "000002.SZ",
        "rsi_reversal",
        100000.0,
        100100.0,
        [_fill(Side.BUY, 100, 20.0, day1), _fill(Side.SELL, 100, 21.0, day2)],
        path=db,
    )

    assert paper_ledger.get_positions(path=db) == [
        {"symbol": "000001.SZ", "quantity": 100}
    ]


def test_env_var_path(monkeypatch, tmp_path: Path):
    db = tmp_path / "env-ledger.db"
    monkeypatch.setenv("TRADEPILOT_BACKTEST_DB", str(db))

    paper_ledger.record_run(
        "run-env", "600519.SH", "ma_cross", 50000.0, 51000.0, []
    )

    assert db.exists()
    assert [run["run_id"] for run in paper_ledger.list_runs()] == ["run-env"]


def test_paper_trade_ledger_records_run(monkeypatch, tmp_path: Path):
    db = tmp_path / "exec-ledger.db"
    monkeypatch.setenv("TRADEPILOT_BACKTEST_DB", str(db))

    result = paper_trade(
        _RoundTripStrategy(),
        _bars(),
        starting_cash=100000.0,
        ledger=True,
        run_id="exec-run",
        symbol="600000.SH",
    )

    assert len(result.fills) == 2
    runs = paper_ledger.list_runs(path=db)
    assert [run["run_id"] for run in runs] == ["exec-run"]
    assert runs[0]["symbol"] == "600000.SH"
    assert runs[0]["strategy"] == "round_trip"
    assert runs[0]["initial_cash"] == 100000.0
    assert runs[0]["final_equity"] == result.equity_curve[-1]

    stored = paper_ledger.get_run_fills("exec-run", path=db)
    assert [fill["side"] for fill in stored] == [
        fill.side.value for fill in result.fills
    ]
    assert sum(fill["quantity"] for fill in stored) == sum(
        int(fill.quantity) for fill in result.fills
    )
    # 买完又全卖 → 净持仓清零
    assert paper_ledger.get_positions(path=db) == []


def test_paper_trade_default_does_not_touch_ledger(monkeypatch, tmp_path: Path):
    db = tmp_path / "no-ledger.db"
    monkeypatch.setenv("TRADEPILOT_BACKTEST_DB", str(db))

    result = paper_trade(_RoundTripStrategy(), _bars())

    assert len(result.fills) == 2
    assert not db.exists()
