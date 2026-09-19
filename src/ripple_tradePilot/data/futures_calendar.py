"""期货交易日历与夜盘归属（纯函数，无网络、无 DB）。

WHY 用「主力品种日线日期集」当日历：中国期货无官方免费交易日历 API，
而交易所级假期在 K 线上天然表现为断流日（P0 §4.4 实证：RB 229 个交易日
覆盖全年，假期自动缺席）。因此 ``TradingCalendar`` 由调用方灌入某主力
品种日线的日期集合，覆盖范围内「不在集合 = 休市」；超出首末日期范围时
退化为工作日近似，并把近似状态显式透出（``state()["is_approx"]``），
绝不用静默默认值掩盖。

夜盘归属（``trade_date_of``）是 P0 §4.2 的实证结论，必须钉死：
新浪对跨零点 bar 的标签口径不一致——cu2610 同一夜盘（周五 2026-09-18 晚）
的 bar 同时存在自然日标签 ``2026-09-19 01:00``（周六凌晨）与交易日标签
``2026-09-21 00:00``（周一）。所以**不得信任标签日期**，只按小时归交易
日，两种标签口径在该规则下都归到正确的交易日 2026-09-21。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, Optional, Tuple

from ripple_tradePilot.data.futures_meta import ProductSpec

# 向前/向后找交易日的安全窗：覆盖春节+国庆连休（约 8~10 个自然日）绰绰有余，
# 只用于防死循环；真实日历不可能连续 40 个自然日无交易日。
_MAX_LOOKAHEAD_DAYS = 40


def _parse_hhmm(text: str) -> int:
    """``"21:00"`` → 分钟数 1260；供夜盘时段比较（左闭右开）。"""
    hour, minute = str(text).strip().split(":")
    return int(hour) * 60 + int(minute)


class TradingCalendar:
    """由交易日日期集合构成的交易所日历（不可变数据 + 近似状态标记）。

    三段语义：
    - 日期在集合内 → 交易日；
    - 日期在首末覆盖范围内但不在集合 → 休市（假期由断流自然体现）；
    - 日期超出覆盖范围 → 工作日近似（周一~周五视为交易日），并置位近似
      标记。标记是**粘性的**：一旦有查询落到近似路径就保持 True，调用方
      在批处理后读 ``state()`` 即可知道结果是否被近似污染（P0 §4.4 要求
      显式告警而非静默）。
    """

    def __init__(self, trading_dates: Iterable[date]):
        dates = frozenset(trading_dates)
        if dates:
            self._dates: frozenset = dates
            self._first: Optional[date] = min(dates)
            self._last: Optional[date] = max(dates)
            self._approx_used = False
        else:
            # 空日历本身就是纯近似：任何查询都只能走工作日猜测
            self._dates = dates
            self._first = None
            self._last = None
            self._approx_used = True

    def covers(self, day: date) -> bool:
        """日期是否落在 [首日, 末日] 覆盖范围内（与是否交易日无关）。"""
        if self._first is None or self._last is None:
            return False
        return self._first <= day <= self._last

    def is_trading_day(self, day: date) -> bool:
        if day in self._dates:
            return True
        if self.covers(day):
            # 覆盖范围内的断流 = 休市（假期），这是确定结论，不是近似
            return False
        self._approx_used = True
        return day.weekday() < 5  # 近似：工作日视为交易日

    def next_trading_day(self, day: date) -> date:
        """严格晚于 ``day`` 的下一个交易日（不含自身）。

        夜盘归属需要「不含自身」语义：周五 21:00 后的 bar 属于周五的下一
        个交易日（下周一，P0 §4.2 实证）；凌晨 bar 的「含自身」判断由
        ``trade_date_of`` 组合 ``is_trading_day`` 完成。
        """
        cursor = day + timedelta(days=1)
        for _ in range(_MAX_LOOKAHEAD_DAYS):
            if self.covers(cursor):
                if cursor in self._dates:
                    return cursor
            else:
                # 超出覆盖范围：工作日近似，并显式置位
                self._approx_used = True
                if cursor.weekday() < 5:
                    return cursor
            cursor += timedelta(days=1)
        raise ValueError(
            f"从 {day} 起 {_MAX_LOOKAHEAD_DAYS} 天内找不到交易日（日历数据异常）"
        )

    def state(self) -> Dict[str, Any]:
        """透出近似状态：{"covered_range": (first, last) 或 None, "is_approx": bool}。"""
        covered: Optional[Tuple[date, date]] = (
            None if self._first is None or self._last is None
            else (self._first, self._last)
        )
        return {"covered_range": covered, "is_approx": self._approx_used}


def trade_date_of(ts: datetime, spec: ProductSpec, calendar: TradingCalendar) -> date:
    """bar 时间戳 → 所属交易日（P0 §4.2 实证规则，钉死勿改）。

    - ``hour >= 21`` → 下一交易日（不含当日）：夜盘开市后的 bar 属于
      下一个交易日（含周五晚 → 下周一）；
    - ``hour < 4`` → 从自然日起的下一交易日（**含自身**）：凌晨 bar 要么
      属于当日（若当日是交易日，如周一 00:00 标签），要么属于从该自然日
      起的下一交易日（如周六凌晨标签 → 周一）；
    - 其余（4 <= hour < 21）→ 自然日本身（日盘时段，应为交易日）。

    该规则使 cu 的两种跨零点标签口径（2026-09-19 01:00 与 2026-09-21 00:00）
    都归到交易日 2026-09-21。``spec`` 当前不参与判断（五个首期品种的夜盘
    时段不影响小时规则），保留在签名里与 P0 §6 接口草案一致。
    """
    if ts.hour >= 21:
        return calendar.next_trading_day(ts.date())
    if ts.hour < 4:
        day = ts.date()
        if calendar.is_trading_day(day):
            return day
        return calendar.next_trading_day(day)
    return ts.date()


def is_night_session(ts: datetime, spec: ProductSpec) -> bool:
    """时间戳是否处于 ``spec`` 的夜盘时段（左闭右开 [start, end)）。

    跨零点品种（cu，``night_end_next_day=True``，收于次日 01:00）的时段
    等价于 ``t >= 21:00 或 t < 01:00``；无夜盘品种（night_start 为空）
    恒为 False。恰好等于收市时刻的时间戳不算交易中（bar 标签=结束时刻，
    23:00 标签的 bar 在 23:00 已收线）。
    """
    if not spec.night_start or not spec.night_end:
        return False
    minutes = ts.hour * 60 + ts.minute
    start = _parse_hhmm(spec.night_start)
    end = _parse_hhmm(spec.night_end)
    if spec.night_end_next_day:
        return minutes >= start or minutes < end
    return start <= minutes < end
