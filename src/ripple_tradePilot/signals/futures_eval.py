"""期货信号倾向语义（roadmap P1「明确信号含义」行）。

P1 语义原文：「没有账户持仓时输出做多倾向、做空倾向或观望；有持仓上下文后
才能生成开仓、平仓动作」。本模块只输出**倾向**（tilt），永不输出委托、方向
动作或目标持仓——那是 P2 引入持仓上下文之后的事。

与现有代码的关系（避免第二套口径）：

- ATR **必须复用** :mod:`ripple_tradePilot.indicators` 的 ``atr_series``（TR 的
  period 简单均值，首根 TR = high−low，无前收）。接口适配：传入全量升序
  highs/lows/closes，取序列末值作为最新完成 bar 的 ATR。注意这不是 Wilder
  递推口径——沿用 indicators 既有口径是为了与 dashboard/回测共享同一数字。
- 通道复用 indicators 的 ``rolling_max`` / ``rolling_min``（模块内注释即
  「唐奇安通道上/下轨用」）。「不含当前 bar」通过取序列倒数第二个位置实现：
  ``rolling_max(h, N)[-2] == max(h[:-1][-N:])``。
- ``strategies/donchian.py`` 的 DonchianBreakout 是带状态的股票策略（BUY/SELL
  + 信号去重状态机），语义与"无持仓倾向"不同，故不直接复用其状态机，只沿用
  同一突破判定：收盘价 vs 此前 N 根高/低点。

纯函数保证：无随机性、无时钟、无 IO，**同输入必同输出**——通知层的持久化
去重键（合约+周期+信号时间+策略版本）依赖这一性质。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..data.futures_meta import RULE_VERSION
from ..indicators import atr_series, rolling_max, rolling_min
from ..models.types import FuturesDirection

__all__ = [
    "DonchianParams",
    "FuturesTilt",
    "STRATEGY_KEY",
    "TILT_LONG",
    "TILT_NEUTRAL",
    "TILT_SHORT",
    "evaluate_tilt",
    "tilt_series",
]

#: 倾向常量（非订单方向）。LONG/SHORT 直接取 FuturesDirection 枚举值，
#: 保证 P2 引入持仓上下文后「倾向 × 持仓 → 开/平动作」的方向语义无缝衔接；
#: NEUTRAL（观望）不是方向，无对应枚举成员。
TILT_LONG = FuturesDirection.LONG.value    # "LONG"
TILT_SHORT = FuturesDirection.SHORT.value  # "SHORT"
TILT_NEUTRAL = "NEUTRAL"

#: 默认参数键名（strategy_key 之外的稳定标识）
STRATEGY_KEY = "donchian"


@dataclass(frozen=True)
class DonchianParams:
    entry_window: int = 20       # 突破窗（此前 N 根，不含当前 bar）
    exit_window: int = 10        # 展示用反向通道（本阶段仅信息，不产生动作）
    atr_period: int = 14
    atr_stop_multiple: float = 2.0


@dataclass(frozen=True)
class FuturesTilt:
    """一次倾向评估的结果快照（纯数据，可直接作为通知/存储行）。"""

    symbol: str
    timeframe: str               # '60m'
    tilt: str                    # TILT_LONG / TILT_SHORT / TILT_NEUTRAL
    basis: str                   # 人话依据，含通道数值与突破价
    channel_high: float          # 最近 entry_window 根完成 bar 的最高（不含当前）
    channel_low: float           # 同上口径的最低
    close: float
    atr: float
    stop_distance: float         # atr × atr_stop_multiple（价格单位）
    stop_ref_price: float        # LONG: close−距离; SHORT: close+距离; NEUTRAL: 0
    as_of: str                   # 最后一根完成 bar 的时间（bar_time 原样）
    strategy_key: str            # 'donchian'
    strategy_version: str        # f"donchian{entry}/{exit}+atr{p}x{m}@{RULE_VERSION}"


def _series(bars: Sequence[dict], key: str) -> List[float]:
    values: List[float] = []
    for bar in bars:
        try:
            values.append(float(bar[key]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"bar 行缺少或非法字段 {key!r}: {bar!r}") from exc
    return values


def _validate_params(params: DonchianParams) -> None:
    if params.entry_window < 1:
        raise ValueError(f"entry_window 需 ≥ 1（当前 {params.entry_window}）")
    if params.exit_window < 1:
        raise ValueError(f"exit_window 需 ≥ 1（当前 {params.exit_window}）")
    if params.atr_period < 1:
        raise ValueError(f"atr_period 需 ≥ 1（当前 {params.atr_period}）")
    if not params.atr_stop_multiple > 0:
        raise ValueError(f"atr_stop_multiple 需 > 0（当前 {params.atr_stop_multiple}）")


def _build_tilt(
    bars: Sequence[dict],
    index: int,
    params: DonchianParams,
    *,
    symbol: str,
    timeframe: str,
    highs: List[float],
    lows: List[float],
    closes: List[float],
    atr: float,
    channel_high: float,
    channel_low: float,
) -> FuturesTilt:
    """从预计算的序列值构造第 ``index`` 根 bar 的倾向快照。

    ``evaluate_tilt``（末位）与 ``tilt_series``（全序列）共用的字段构造逻辑
    ——单一实现保证两条路径逐位一致（含 basis 字符串）。``channel_high``/
    ``channel_low`` 须为「不含当前 bar 的此前 entry_window 根极值」口径。
    """
    close = closes[index]

    stop_distance = atr * params.atr_stop_multiple
    if close > channel_high:
        tilt = TILT_LONG
        stop_ref_price = close - stop_distance
        basis = (
            f"收盘 {close:g} 突破此前 {params.entry_window} 根最高 {channel_high:g}"
            f"；ATR{params.atr_period}={atr:.4g}，止损距离 {stop_distance:.4g}"
            f"（参考价 {stop_ref_price:.4g}）"
        )
    elif close < channel_low:
        tilt = TILT_SHORT
        stop_ref_price = close + stop_distance
        basis = (
            f"收盘 {close:g} 跌破此前 {params.entry_window} 根最低 {channel_low:g}"
            f"；ATR{params.atr_period}={atr:.4g}，止损距离 {stop_distance:.4g}"
            f"（参考价 {stop_ref_price:.4g}）"
        )
    else:
        tilt = TILT_NEUTRAL
        stop_ref_price = 0.0  # 无倾向即无止损方向，置 0 并在 basis 说明
        basis = (
            f"收盘 {close:g} 位于此前 {params.entry_window} 根区间"
            f" [{channel_low:g}, {channel_high:g}] 之内，观望"
            "；无倾向故不设止损参考（stop_ref_price=0）"
        )

    last_bar = bars[index]
    if "bar_time" not in last_bar:
        raise ValueError(f"最新 bar 缺少 bar_time 字段: {last_bar!r}")

    return FuturesTilt(
        symbol=str(symbol),
        timeframe=timeframe,
        tilt=tilt,
        basis=basis,
        channel_high=channel_high,
        channel_low=channel_low,
        close=close,
        atr=atr,
        stop_distance=stop_distance,
        stop_ref_price=stop_ref_price,
        as_of=str(last_bar["bar_time"]),
        strategy_key=STRATEGY_KEY,
        strategy_version=(
            f"donchian{params.entry_window}/{params.exit_window}"
            f"+atr{params.atr_period}x{params.atr_stop_multiple}@{RULE_VERSION}"
        ),
    )


def evaluate_tilt(
    bars: List[dict],
    params: DonchianParams,
    *,
    symbol: str,
    timeframe: str = "60m",
) -> Optional[FuturesTilt]:
    """已完成 K 线 → 期货倾向（LONG/SHORT/NEUTRAL）。

    bars 契约（调用方负责，本函数不做补全/排序/过滤）：

    - **升序已完成 bar**：``[{bar_time: str, open, high, low, close, ...}, ...]``，
      最新一根在末尾；volume/hold 等额外字段可有可无；
    - 只喂已完成 bar——未完成 bar 的 close 会随时间变动，倾向必须建立在
      收盘定型的数据上（P1「只使用已完成 K 线」）。

    语义：``close > max(high[:-1][-N:])`` → LONG；``close < min(low[:-1][-N:])``
    → SHORT；否则 NEUTRAL（含恰好触及通道边界：突破是**严格**大于/小于）。

    返回 ``None`` 与返回 NEUTRAL 含义不同：

    - ``None`` = 数据不足（``len(bars) < entry_window + 1`` 或 ATR 周期不够），
      即"没法算"——调用方不应据此发通知，也不应视为观望；
    - NEUTRAL = 算得出来，结论是观望。

    参数非法 / bar 行缺关键字段 → ValueError（调用方 bug，明确暴露而非吞掉）。
    纯函数：同输入同输出，通知去重键依赖此性质。

    监控等单次评估场景用本函数；回测/网格等需要**每个前缀**的倾向时用
    :func:`tilt_series`（O(n) 一次算完，勿在本函数上按前缀循环——那是 O(n²)）。
    """
    _validate_params(params)
    if len(bars) < params.entry_window + 1:
        return None

    highs = _series(bars, "high")
    lows = _series(bars, "low")
    closes = _series(bars, "close")

    # ATR：复用 indicators.atr_series（TR 简单均值口径），取末值即最新完成 bar
    atr = atr_series(highs, lows, closes, params.atr_period)[-1]
    if atr is None:
        return None  # ATR 周期不够：倾向必须有止损距离才有意义，否则"没法算"

    # 通道：rolling_max/min 的倒数第二位 = 不含当前 bar 的此前 N 根极值
    channel_high = rolling_max(highs, params.entry_window)[-2]
    channel_low = rolling_min(lows, params.entry_window)[-2]
    if channel_high is None or channel_low is None:
        return None  # 窗口未满（长度门控之外的防御位）
    return _build_tilt(
        bars, len(bars) - 1, params,
        symbol=symbol, timeframe=timeframe,
        highs=highs, lows=lows, closes=closes, atr=atr,
        channel_high=channel_high,
        channel_low=channel_low,
    )


def tilt_series(
    bars: List[dict],
    params: DonchianParams,
    *,
    symbol: str,
    timeframe: str = "60m",
) -> List[Optional[FuturesTilt]]:
    """每个前缀的倾向序列（``result[i] == evaluate_tilt(bars[:i+1])``）。

    全序列一次算完的 O(n) 版本：ATR/唐奇安通道各滚动一遍，第 i 位倾向复用
    已算好的 ``atr_series[i]`` 与 ``rolling_max/min(...)[i-1]``（后者即
    「不含当前 bar 的此前 N 根极值」，与 :func:`evaluate_tilt` 对前缀取
    ``[-2]`` 的口径逐位一致——parity 测试钉死）。字段构造与 evaluate_tilt
    共用 :func:`_build_tilt`，含 basis 字符串逐位相同。

    ``result[i] is None`` 的语义同 evaluate_tilt：数据不足（前缀长度不够
    entry_window+1，或该位 ATR 周期未满），不是观望。

    注意：字段校验是**急切**的——任何一个 bar 缺 high/low/close 都会立刻
    ValueError，而 evaluate_tilt 只在其前缀被实际评估到时才暴露。
    """
    _validate_params(params)
    result: List[Optional[FuturesTilt]] = [None] * len(bars)
    if not bars:
        return result

    highs = _series(bars, "high")
    lows = _series(bars, "low")
    closes = _series(bars, "close")

    atr_all = atr_series(highs, lows, closes, params.atr_period)
    channel_high_all = rolling_max(highs, params.entry_window)
    channel_low_all = rolling_min(lows, params.entry_window)

    # 前缀 bars[:i+1] 够 entry_window+1 根 ⇔ i ≥ entry_window
    for index in range(params.entry_window, len(bars)):
        atr = atr_all[index]
        if atr is None:
            continue  # 该位 ATR 周期未满 = 前缀评估会返回 None
        channel_high = channel_high_all[index - 1]
        channel_low = channel_low_all[index - 1]
        if channel_high is None or channel_low is None:
            continue
        result[index] = _build_tilt(
            bars, index, params,
            symbol=symbol, timeframe=timeframe,
            highs=highs, lows=lows, closes=closes, atr=atr,
            channel_high=channel_high,
            channel_low=channel_low,
        )
    return result
