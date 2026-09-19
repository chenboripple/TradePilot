"""期货行情适配层（P1）：新浪源归一化 + 缺口/新鲜度检查 + 主力判定 + 快照。

纯数据归一化，不依赖 DB（store 以鸭子类型注入，真实落库实现后续接入）。
口径依据 docs/futures-p0-verification.md：

- §2  数据源：主源 = akshare 新浪（``futures_zh_daily_sina`` 日线 +
      ``futures_zh_minute_sina`` 60m），辅源 ``futures_comm_info`` 快照
      （现价/涨跌停/每手保证金/手续费）；郑商所空响应 → 首期品种外整行跳过。
- §4.2 夜盘归属：不信任跨零点 bar 的标签日期，统一交给
      ``futures_calendar.trade_date_of`` 归交易日（本模块只管数据，不重复实现规则）。
- §4.3 成交量口径：只有日线的 ``volume/hold`` 可信（60m 与日线两路聚合
      无法逐笔对账），本层**不做任何系数修正**；量能/持仓判断在下游只走日线。
- §4.4 交易日历：调用方传入由主力品种日线日期集构建的 ``TradingCalendar``。
- §5  主力判定：最新**已收盘共同**交易日上 ``volume`` 最大者，``hold``
      并列参考；连续符号（RB0）只用于研究口径，不用它判主力。

akshare 在方法内**惰性导入**：本模块 import 不拉起重依赖、可完全离线测试，
测试用 ``patch.dict(sys.modules, {"akshare": fake})`` 注入假模块（见
tests/test_futures_data.py，手法对应 test_stock_service.py 对 ak 函数的 patch）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Protocol

import pandas as pd

from ripple_tradePilot.data.futures_calendar import (
    TradingCalendar,
    trade_date_of,
)
from ripple_tradePilot.data.futures_meta import (
    ContractIdentity,
    parse_contract_symbol,
    product_spec,
)

logger = logging.getLogger(__name__)

#: 时间周期标识（与存储层的 timeframe 键一致）
TIMEFRAME_DAILY = "1d"
TIMEFRAME_60M = "60m"

#: 60m 新鲜度容忍：距最近应有 bar 结束时刻超过 2 根 bar 周期（2 小时）才判过期，
#: 容差用于吸收小时栅格近似（午休 11:30–13:30、10:15–10:30 小节休息造成的
#: 11:15/14:15 等错位聚合标签，P0 §4.1），避免误报。
_STALE_BAR_PERIODS = 2

#: 日盘时段（五品种一致的 09:00–15:00 含午休；小时栅格近似，见 _STALE_BAR_PERIODS）
_DAY_SESSION_START = time(9, 0)
_DAY_SESSION_END = time(15, 0)


class FuturesDataError(RuntimeError):
    """期货数据层基础错误。"""


class FuturesDataUnavailableError(FuturesDataError):
    """上游数据源不可用或返回空（消息含品种与原因）。"""


@dataclass(frozen=True)
class Freshness:
    """新鲜度结论：fresh + 期望锚点 + 人话说明。"""

    fresh: bool
    expected: str  # 日线：'YYYYMMDD'；60m：最近应有 bar 结束时刻 'YYYY-MM-DD HH:MM:SS'；休市时为 ''
    detail: str


@dataclass(frozen=True)
class SyncReport:
    """sync_contract 的结果汇报。"""

    symbol: str  # canonical，如 "RB2610.SHFE"
    timeframe: str
    rows_written: int  # 本次 upsert 新写入的行数（store 语义：幂等覆盖，重复行不计）
    gaps: List[str]  # 首末之间日历有而序列缺的交易日（YYYYMMDD）
    freshness: Freshness
    new_bars: int  # 比库内原最新 bar 更新的行数（sync 前后 diff 口径）


class _Store(Protocol):
    """落库协议（鸭子类型）：真实实现由存储层后续接入，本模块只依赖这两个方法。"""

    def latest_bar(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        """返回库内该合约该周期的最新一行（含 trade_date/bar_time），无数据返回 None。"""
        ...

    def upsert_bars(self, timeframe: str, rows: List[Dict[str, Any]]) -> int:
        """幂等写入（按 symbol+trade_date+bar_time 覆盖），返回**新写入**的行数。"""
        ...


def _parse_hhmm(text: str) -> time:
    hour, minute = str(text).strip().split(":")
    return time(int(hour), int(minute))


class FuturesDataService:
    """新浪期货行情的归一化/检查/同步服务（store 可注入；None = 仅归一化不落库）。"""

    def __init__(self, store: Optional[_Store] = None):
        self.store = store

    # ------------------------------------------------------------------ #
    # 归一化（纯函数式，输入合成 DataFrame 即可离线测试）                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        """宽松数值化（NaN/脏值 → None）；与 stock_service 同一口径。"""
        if value is None:
            return None
        try:
            number = float(str(value).replace(",", "").replace("%", ""))
        except (TypeError, ValueError):
            return None
        return None if pd.isna(number) else number

    @staticmethod
    def _identity(canonical: str) -> ContractIdentity:
        """canonical（如 RB2610.SHFE）或裸代码 → ContractIdentity。"""
        return parse_contract_symbol(str(canonical).split(".")[0])

    @staticmethod
    def _sanity_filter(data: pd.DataFrame) -> pd.DataFrame:
        """价格合理性：丢非正价/高低倒置的脏行（沿用股票侧口径，防脏数据进信号）。"""
        if not len(data):
            return data
        return data[
            (data[["open", "high", "low", "close"]] > 0).all(axis=1)
            & (data["high"] >= data["low"])
        ]

    def normalize_daily(self, df: pd.DataFrame, canonical: str) -> List[Dict[str, Any]]:
        """新浪日线 → 标准行。

        输入列（P0 §2 实测）：``date,open,high,low,close,volume,hold,settle``
        （date 'YYYY-MM-DD'；volume=成交量，hold=持仓量，settle=结算价）。
        输出行：{symbol, trade_date:'YYYYMMDD', open/high/low/close/volume/hold/
        settle(float), bar_time:'', source:'sina'}；按日期升序、同日去重保留
        后到（新浪偶发补发/修正行，后到为准）。
        """
        identity = self._identity(canonical)
        if df is None or len(df) == 0:
            return []
        data = df.copy()
        for required in ("date", "open", "high", "low", "close"):
            if required not in data.columns:
                raise FuturesDataError(
                    f"新浪日线缺少必需列 {required!r}（实际列：{list(data.columns)}）"
                )
        data["trade_date"] = pd.to_datetime(data["date"], errors="coerce").dt.strftime(
            "%Y%m%d"
        )
        for column in ("open", "high", "low", "close", "volume", "hold", "settle"):
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce")
        data = data.dropna(subset=["trade_date", "open", "high", "low", "close"])
        data = self._sanity_filter(data)
        data = (
            data.drop_duplicates(subset=["trade_date"], keep="last")
            .sort_values("trade_date")
            .reset_index(drop=True)
        )
        rows: List[Dict[str, Any]] = []
        for _, row in data.iterrows():
            rows.append(
                {
                    "symbol": identity.canonical,
                    "trade_date": str(row["trade_date"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    # 成交量/持仓缺失按 0 处理（无成交日的常规口径），不做修正
                    "volume": self._optional_float(row.get("volume")) or 0.0,
                    "hold": self._optional_float(row.get("hold")) or 0.0,
                    "settle": self._optional_float(row.get("settle")),
                    "bar_time": "",
                    "source": "sina",
                }
            )
        return rows

    def normalize_60m(
        self, df: pd.DataFrame, canonical: str, calendar: TradingCalendar
    ) -> List[Dict[str, Any]]:
        """新浪 60m → 标准行（trade_date 按 ``trade_date_of`` 归属）。

        输入列（P0 §2 实测）：``datetime,open,high,low,close,volume,hold``
        （datetime 'YYYY-MM-DD HH:MM:SS'，标签 = bar 结束时刻）。日内错位
        聚合标签（11:15/14:15，源自小节休息）**不修正，原样保留 bar_time**
        （P0 §4.1）。trade_date 由小时规则重算，天然消化跨零点双标签口径
        （P0 §4.2，cu 案例见测试）。输出按 (trade_date, bar_time) 去重
        （保留后到）并升序；bar_time 字符串即时间序，同 trade_date 内顺序
        确定即可，已知缺陷：同一夜盘的两种标签在日内相对顺序可能与实际相反，
        不影响 (trade_date, bar_time) 唯一键。
        """
        identity = self._identity(canonical)
        spec = product_spec(identity.product)
        if df is None or len(df) == 0:
            return []
        data = df.copy()
        for required in ("datetime", "open", "high", "low", "close"):
            if required not in data.columns:
                raise FuturesDataError(
                    f"新浪60m缺少必需列 {required!r}（实际列：{list(data.columns)}）"
                )
        data["bar_time"] = data["datetime"].astype(str).str.strip()
        data["_ts"] = pd.to_datetime(data["bar_time"], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "hold"):
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce")
        data = data.dropna(subset=["_ts", "open", "high", "low", "close"])
        data = self._sanity_filter(data)
        # 交易日归属：不信任标签日期，只看小时（P0 §4.2）
        data["trade_date"] = data["_ts"].map(
            lambda ts: trade_date_of(ts.to_pydatetime(), spec, calendar).strftime(
                "%Y%m%d"
            )
        )
        data = (
            data.drop_duplicates(subset=["trade_date", "bar_time"], keep="last")
            .sort_values(["trade_date", "bar_time"])
            .reset_index(drop=True)
        )
        rows: List[Dict[str, Any]] = []
        for _, row in data.iterrows():
            rows.append(
                {
                    "symbol": identity.canonical,
                    "trade_date": str(row["trade_date"]),
                    "bar_time": str(row["bar_time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    # 60m 的 volume/hold 只作展示；量能/持仓判断只用日线（P0 §4.3）
                    "volume": self._optional_float(row.get("volume")) or 0.0,
                    "hold": self._optional_float(row.get("hold")) or 0.0,
                    "settle": None,
                    "source": "sina",
                }
            )
        return rows

    # ------------------------------------------------------------------ #
    # 检查：缺口 / 新鲜度                                                  #
    # ------------------------------------------------------------------ #

    def detect_gaps(
        self, trade_dates: List[str], calendar: TradingCalendar
    ) -> List[str]:
        """首末交易日之间，日历有而序列缺的交易日（'YYYYMMDD'，升序）。

        只查首末之间（序列之前的留白可能是合约晚挂牌，不算缺口）。
        """
        present = sorted(
            {
                value
                for value in trade_dates
                if isinstance(value, str) and len(value) == 8 and value.isdigit()
            }
        )
        if len(present) < 2:
            return []
        first = datetime.strptime(present[0], "%Y%m%d").date()
        last = datetime.strptime(present[-1], "%Y%m%d").date()
        gaps: List[str] = []
        cursor = first
        while True:
            nxt = calendar.next_trading_day(cursor)
            if nxt >= last:
                break
            key = nxt.strftime("%Y%m%d")
            if key not in present:
                gaps.append(key)
            cursor = nxt
        return gaps

    @staticmethod
    def _previous_trading_day(day: date, calendar: TradingCalendar) -> date:
        cursor = day - timedelta(days=1)
        for _ in range(40):
            if calendar.is_trading_day(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise FuturesDataError(f"从 {day} 往前 40 天内找不到交易日（日历数据异常）")

    def freshness_daily(
        self,
        latest_trade_date: str,
        today: date,
        calendar: TradingCalendar,
        now: Optional[datetime] = None,
    ) -> Freshness:
        """日线新鲜度：应覆盖到「今天之前的最后交易日」。

        交易日语义与 ``trade_date_of`` 同一套小时规则（与品种无关，五个首期
        品种夜盘都是 21:00 开）：
        - 夜盘已开的当晚（today 为交易日且 now >= 21:00）：当前交易日已是
          **下一**个交易日，日线只需覆盖到上一交易日（= today 本身，其日盘
          已于 15:00 收线）——允许数据「仍停在上一交易日」；
        - 盘中/盘后（4 <= hour < 21）：当前交易日 = today，当日日线未收线，
          期望仍是严格早于 today 的最后交易日（保守口径，不误报）；
        - 凌晨（hour < 4）：夜盘 bar 归入 today 的日线，当日日线同样未收线。

        ``now`` 缺省取系统当前时间（生产路径）；测试注入固定时刻保持离线
        确定性——夜盘窗口判定需要**时刻**而非日期，这是对提示签名的必要补充。
        """
        if now is None:
            now = datetime.now()
        if now.hour >= 21:
            current = calendar.next_trading_day(today)
        elif now.hour < 4:
            current = (
                today
                if calendar.is_trading_day(today)
                else calendar.next_trading_day(today)
            )
        else:
            current = today
        expected = self._previous_trading_day(current, calendar)
        expected_str = expected.strftime("%Y%m%d")
        if not latest_trade_date:
            return Freshness(
                False, expected_str, f"库内无日线数据；期望覆盖至 {expected_str}"
            )
        fresh = str(latest_trade_date) >= expected_str
        night_note = (
            "（当前处于夜盘，当前交易日为次日，日线覆盖到上一交易日即可）"
            if current != today
            else "（当日日线未收线，期望停在上一交易日）"
        )
        detail = (
            f"最新 {latest_trade_date}，期望 >= {expected_str}{night_note}"
        )
        return Freshness(fresh, expected_str, ("日线新鲜：" if fresh else "日线过期：") + detail)

    @staticmethod
    def _night_session_on(
        evening: date, calendar: TradingCalendar
    ) -> bool:
        """当晚（evening 21:00 起）是否有夜盘——近似口径。

        规则：evening 是交易日，且次日是周末或交易日。周五晚有夜盘、计入
        下一交易日（下周一，P0 §4.2 实证）；长假前夜（次日既非周末也非交易
        日，如国庆前）夜盘休市。边界情形出错只影响新鲜度提示的噪音，不影响
        数据正确性，故用该近似而不是引入官方假期表。
        """
        if not calendar.is_trading_day(evening):
            return False
        nxt = evening + timedelta(days=1)
        return nxt.weekday() >= 5 or calendar.is_trading_day(nxt)

    def _active_session_window(
        self, now: datetime, spec, calendar: TradingCalendar
    ) -> Optional[tuple]:
        """now 所处的交易时段 (start, end, label)；休市返回 None。

        日盘 09:00–15:00（含午休，小时栅格近似）；夜盘按 spec 的
        night_start/night_end（跨零点品种窗口延到次日，cu 到 01:00）。
        """
        today = now.date()
        if calendar.is_trading_day(today):
            day_start = datetime.combine(today, _DAY_SESSION_START)
            day_end = datetime.combine(today, _DAY_SESSION_END)
            if day_start <= now < day_end:
                return (day_start, day_end, "日盘")
        if spec.night_start and spec.night_end:
            night_start = _parse_hhmm(spec.night_start)
            night_end = _parse_hhmm(spec.night_end)
            for evening in (today, today - timedelta(days=1)):
                if not self._night_session_on(evening, calendar):
                    continue
                start = datetime.combine(evening, night_start)
                end_day = evening + timedelta(days=1) if spec.night_end_next_day else evening
                end = datetime.combine(end_day, night_end)
                if start <= now < end:
                    return (start, end, "夜盘")
        return None

    def freshness_60m(
        self,
        latest_bar_time: Optional[datetime],
        now: datetime,
        spec,
        calendar: TradingCalendar,
    ) -> Freshness:
        """60m 新鲜度：距最近应有的 bar 结束时刻超 2 根 bar 周期 → stale。

        - 交易时段内：以小时整点栅格近似「最近应有 bar 结束时刻」（午休/
          小节休息的错位聚合由 2 根容忍吸收，见 _STALE_BAR_PERIODS）；
        - 休市 / 夜盘未开 / 日盘已收：恒 fresh，detail 注明休市（此时无从
          要求更新，硬查只会误报）。
        """
        window = self._active_session_window(now, spec, calendar)
        if window is None:
            return Freshness(
                True, "", "休市/夜盘未开：当前不在交易时段，不要求 60m 数据更新"
            )
        start, _, label = window
        ref = now.replace(minute=0, second=0, microsecond=0)
        if ref < start:
            ref = start  # 开市未满一小时：以开市时刻为基准（首根 bar 尚未收线）
        expected = ref.strftime("%Y-%m-%d %H:%M:%S")
        ref_dt = ref
        if latest_bar_time is None:
            return Freshness(False, expected, f"{label}进行中但无 60m 数据")
        lag = ref_dt - latest_bar_time
        tolerance = timedelta(minutes=60) * _STALE_BAR_PERIODS
        if lag > tolerance:
            return Freshness(
                False,
                expected,
                f"{label}进行中：最新 bar {latest_bar_time.strftime('%Y-%m-%d %H:%M:%S')}"
                f" 落后应有结束时刻 {expected} 超过 {_STALE_BAR_PERIODS} 根 bar 周期",
            )
        return Freshness(
            True,
            expected,
            f"{label}进行中：最新 bar {latest_bar_time.strftime('%Y-%m-%d %H:%M:%S')}"
            f" 距 {expected} 未超过 {_STALE_BAR_PERIODS} 根 bar 周期",
        )

    # ------------------------------------------------------------------ #
    # 拉取（akshare 惰性导入；空表/异常 → FuturesDataUnavailableError）      #
    # ------------------------------------------------------------------ #

    def fetch_daily(self, symbol: str) -> List[Dict[str, Any]]:
        """真实合约日线：parse → akshare 新浪 → normalize；空表 raise。"""
        identity = parse_contract_symbol(symbol)
        import akshare as ak  # 惰性导入：见模块 docstring

        try:
            frame = ak.futures_zh_daily_sina(symbol=identity.symbol)
        except Exception as error:
            raise FuturesDataUnavailableError(
                f"{identity.symbol} 日线获取失败（新浪源）：{error}"
            ) from error
        rows = self.normalize_daily(frame, identity.canonical)
        if not rows:
            raise FuturesDataUnavailableError(
                f"{identity.symbol} 新浪日线返回空数据（合约未挂牌/已摘牌，或源故障——"
                "郑商所品种为已知空响应，见 P0 §2）"
            )
        return rows

    def fetch_60m(
        self, symbol: str, calendar: TradingCalendar
    ) -> List[Dict[str, Any]]:
        """真实合约 60m：parse → akshare 新浪（period='60'）→ normalize；空表 raise。"""
        identity = parse_contract_symbol(symbol)
        import akshare as ak

        try:
            frame = ak.futures_zh_minute_sina(symbol=identity.symbol, period="60")
        except Exception as error:
            raise FuturesDataUnavailableError(
                f"{identity.symbol} 60m获取失败（新浪源）：{error}"
            ) from error
        rows = self.normalize_60m(frame, identity.canonical, calendar)
        if not rows:
            raise FuturesDataUnavailableError(
                f"{identity.symbol} 新浪60m返回空数据（合约未挂牌/已摘牌，或源故障）"
            )
        return rows

    # ------------------------------------------------------------------ #
    # 同步（store 鸭子接口；幂等覆盖）                                       #
    # ------------------------------------------------------------------ #

    def sync_contract(
        self,
        symbol: str,
        timeframe: str,
        calendar: TradingCalendar,
        now: Optional[datetime] = None,
    ) -> SyncReport:
        """比对库内最新 → 全量 fetch → upsert（幂等覆盖）→ 缺口 + 新鲜度。

        全量拉取而非增量：新浪接口不支持起始日期参数，且 60m 历史仅 ~8.5 个月
        （P0 §2），全量成本低；upsert 按 (symbol, trade_date, bar_time) 覆盖，
        重复 sync 不会产生重复行。``now`` 缺省取当前时间，测试注入固定时刻。
        """
        if self.store is None:
            raise FuturesDataError(
                "sync_contract 需要注入 store（latest_bar/upsert_bars 鸭子接口）；"
                "仅做归一化请直接用 fetch_daily/fetch_60m"
            )
        if timeframe not in (TIMEFRAME_DAILY, TIMEFRAME_60M):
            raise ValueError(
                f"不支持的时间周期 {timeframe!r}（可选 {TIMEFRAME_DAILY}/{TIMEFRAME_60M}）"
            )
        if now is None:
            now = datetime.now()
        identity = self._identity(symbol)  # 兼容裸代码与 canonical（RB2610 / RB2610.SHFE）
        latest = self.store.latest_bar(identity.canonical, timeframe)
        if timeframe == TIMEFRAME_DAILY:
            rows = self.fetch_daily(identity.symbol)
            latest_data_date = rows[-1]["trade_date"]
            freshness = self.freshness_daily(
                latest_data_date, now.date(), calendar, now=now
            )
        else:
            rows = self.fetch_60m(identity.symbol, calendar)
            latest_data_dt = (
                datetime.strptime(rows[-1]["bar_time"], "%Y-%m-%d %H:%M:%S")
                if rows
                else None
            )
            freshness = self.freshness_60m(
                latest_data_dt, now, product_spec(identity.product), calendar
            )
        rows_written = self.store.upsert_bars(timeframe, rows)
        gaps = self.detect_gaps([row["trade_date"] for row in rows], calendar)
        if latest is None:
            new_bars = len(rows)
        else:
            latest_key = (
                str(latest.get("trade_date") or ""),
                str(latest.get("bar_time") or ""),
            )
            new_bars = sum(
                1
                for row in rows
                if (row["trade_date"], row["bar_time"]) > latest_key
            )
        logger.debug(
            "sync %s %s：写入 %d 行（新 %d），缺口 %s，新鲜度 %s",
            identity.canonical, timeframe, rows_written, new_bars, gaps, freshness.detail,
        )
        return SyncReport(
            symbol=identity.canonical,
            timeframe=timeframe,
            rows_written=rows_written,
            gaps=gaps,
            freshness=freshness,
            new_bars=new_bars,
        )

    # ------------------------------------------------------------------ #
    # 快照 / 主力 / 到期                                                   #
    # ------------------------------------------------------------------ #

    def refresh_quote_snapshot(self) -> List[Dict[str, Any]]:
        """``futures_comm_info`` 快照 → 只留首期品种内的合约行。

        真实列名（akshare 1.18.40，九期网）：合约代码（**小写**，如 rb2701）/
        现价/涨停板/跌停板/保证金-每手/手续费(开+平)/备注（主力合约标记）/
        价格更新时间。保证金与手续费是**某期货公司口径**，仅作风险测算参考
        （P0 §3）；单行缺列/解析失败只跳过该行，不炸整体。
        """
        import akshare as ak

        try:
            frame = ak.futures_comm_info()
        except Exception as error:
            raise FuturesDataUnavailableError(
                f"合约快照获取失败（futures_comm_info）：{error}"
            ) from error
        if frame is None or len(frame) == 0 or "合约代码" not in frame.columns:
            raise FuturesDataUnavailableError("合约快照为空或缺少「合约代码」列")
        rows: List[Dict[str, Any]] = []
        for _, row in frame.iterrows():
            try:
                identity = parse_contract_symbol(str(row.get("合约代码") or ""))
            except ValueError:
                continue  # 非首期品种（郑商所/能源中心/股指等）→ 整行过滤
            price = self._optional_float(row.get("现价"))
            if price is None:
                continue  # 现价缺失（未挂牌/长期无成交）→ 跳过该行
            rows.append(
                {
                    "symbol": identity.canonical,
                    "price": price,
                    "upper_limit": self._optional_float(row.get("涨停板")),
                    "lower_limit": self._optional_float(row.get("跌停板")),
                    "margin_per_hand": self._optional_float(row.get("保证金-每手")),
                    "fee_per_lot": self._optional_float(row.get("手续费")),
                    "is_main": str(row.get("备注") or "").strip() == "主力合约",
                    "price_time": str(row.get("价格更新时间") or "").strip(),
                    "source": "comm_info",
                }
            )
        return rows

    def resolve_main(
        self, daily_rows_by_symbol: Dict[str, List[Dict[str, Any]]]
    ) -> str:
        """主力判定（P0 §5）：最新**共同**交易日上 volume 最大的 canonical。

        只用已收盘数据（日线），无前视。并列规则：volume 相同取 hold 大者，
        再相同取到期月份更远的（换月后有品种会出现双合约并量）。hold 只是
        并列参考——不作为第一排序键。
        """
        series = {
            symbol: rows
            for symbol, rows in daily_rows_by_symbol.items()
            if rows
        }
        if not series:
            raise FuturesDataError("resolve_main 需要至少一个有日线数据的合约")
        date_sets = [{row["trade_date"] for row in rows} for rows in series.values()]
        common = set.intersection(*date_sets)
        if not common:
            raise FuturesDataError("各合约无共同交易日，无法判定主力")
        latest = max(common)

        def rank(symbol: str) -> tuple:
            row = next(r for r in series[symbol] if r["trade_date"] == latest)
            identity = self._identity(symbol)
            return (row["volume"], row["hold"], (identity.year, identity.month))

        return max(series, key=rank)

    def near_expiry(
        self,
        identity_year: int,
        identity_month: int,
        today: date,
        buffer_days: int = 30,
    ) -> bool:
        """是否临近到期：距**到期月首日**不足 buffer_days 个日历日 → True。

        近似口径（approximate）：DCE 到期日接口坏（P0 §2），SHFE/DCE 统一以
        到期月首日为锚、不做交易所区分。真实到期日在月中，锚点更早 ⇒ 实际
        余量只会比 buffer_days 更大，该口径偏保守（宁早提醒不漏提醒）。
        已进入到期月（真实到期临近/已过）同样返回 True。
        """
        anchor = date(int(identity_year), int(identity_month), 1)
        return (anchor - today).days <= int(buffer_days)
