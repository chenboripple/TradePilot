"""期货单笔风险测算（roadmap §5「风控与仓位口径」，P1）。

为什么单独成模块：§5 的每手风险 / 可开手数是期货特有口径（合约乘数、每跳盈亏、
保证金占用、往返费用），与股票 100 股一手、T+1 的定仓规则完全不同，必须隔离，
避免两套口径互相污染。

设计约定：

- 纯函数、无 DB/网络：价格、ATR、保证金快照全部由调用方传入，本模块只做算术与
  gate 判断，方便手算案例逐位核对。
- ``risk_report`` 永不抛异常：它的职责就是"不可执行时说清楚为什么"。P1 验收原文：
  行情过期、规则缺失或最小一手超过风险预算时，禁止生成可执行的开仓建议。
  非法输入一律落入 reasons + executable=False，而不是把异常甩给调用方。
- 会**低估风险**的非法输入（负滑点、负手续费、非正止损倍数）直接判不可执行，
  不静默取绝对值或夹 0——风控宁可拒绝也不美化数字；保证金快照异常则回退品种
  缺省率近似估算并注明（近似方向偏保守或中性，允许继续，但必须透出「近似」）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from ..data.futures_meta import (
    ProductSpec,
    margin_estimate,
    notional,
    tick_value,
)

__all__ = ["RiskReport", "SizingParams", "affordable_lots", "per_hand_risk"]


@dataclass(frozen=True)
class SizingParams:
    """单笔定仓参数。随 RiskReport 原样回显——验收要求「注明参数与价格时间」。"""

    risk_budget: float            # 单笔风险预算（元），如 2000
    available_cash: float         # 可用于本次开仓的资金（元）
    atr_stop_multiple: float = 2.0
    slippage_ticks: float = 1.0   # 单边滑点跳数；往返 = 2×该值
    max_lots: Optional[int] = None


@dataclass(frozen=True)
class RiskReport:
    """单品种风险测算快照。executable=False 时 reasons 必非空且含具体数字。"""

    symbol: str
    price: float
    price_time: str               # 价格快照时间（调用方口径原样透出）
    notional_per_hand: float      # 每手名义价值
    margin_per_hand: float        # 每手保证金
    margin_is_estimate: bool      # True=用了 spec 缺省费率而非实时快照
    stop_distance: float          # 止损距离（价格单位）
    stop_distance_pct: float      # 止损距离占价格百分比（60/3104 → 1.93）
    per_hand_risk: float          # = 止损价差×乘数 + 往返费用 + 往返滑点
    lots: int                     # 可开手数
    executable: bool              # 三情形 gate 全过才 True
    reasons: List[str]            # gate 拒绝原因（人话，含数字）
    params: SizingParams          # 参数回显
    rule_version: str             # spec.rule_version 透传；无 spec 为 ""


def per_hand_risk(
    spec: ProductSpec,
    stop_distance_price: float,
    round_trip_fee: float,
    slippage_ticks: float,
) -> float:
    """§5 公式逐字实现：每手风险 = 止损价差 × 合约乘数 + 往返费用 + 2×单边滑点跳数×每跳盈亏。

    纯算术、不做合法性校验（调用方 ``risk_report`` 负责校验；本函数假定入参为
    合法数值）。手算例：rb（乘数 10、每跳 10 元）止损 60、往返费 6.2、单边滑点 1
    → 60×10 + 6.2 + 2×1×10 = 626.2 元。
    """
    return (
        float(stop_distance_price) * spec.multiplier
        + float(round_trip_fee)
        + 2.0 * float(slippage_ticks) * tick_value(spec)
    )


def affordable_lots(
    per_hand_risk_value: float,
    risk_budget: float,
    available_cash: float,
    margin_per_hand: float,
    fee_per_hand: float,
) -> int:
    """可开手数 = 向下取整 min(风险预算/每手风险, 可用资金/(每手保证金+每手费用))。

    负数夹 0。非有限数值、每手风险 ≤ 0 或每手占用（保证金+费用）≤ 0 时返回 0：
    期货保证金恒为正，非正占用意味着入参是垃圾数据，拒绝定仓而不是假设"资金
    不构成约束"。
    """
    values = (per_hand_risk_value, risk_budget, available_cash, margin_per_hand, fee_per_hand)
    if any(value is None or not math.isfinite(value) for value in values):
        return 0
    if per_hand_risk_value <= 0:
        return 0
    by_risk = math.floor(risk_budget / per_hand_risk_value)
    per_hand_cash = margin_per_hand + fee_per_hand
    if per_hand_cash <= 0:
        return 0
    by_cash = math.floor(available_cash / per_hand_cash)
    return max(0, min(by_risk, by_cash))


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive(value: object) -> bool:
    return _is_number(value) and math.isfinite(value) and value > 0  # type: ignore[arg-type]


def _finite(value: object) -> bool:
    return _is_number(value) and math.isfinite(value)  # type: ignore[arg-type]


def _risk_report_impl(
    spec: Optional[ProductSpec],
    symbol: str,
    price: float,
    price_time: str,
    atr: Optional[float],
    params: SizingParams,
    quote_ok: bool,
    margin_per_hand: Optional[float],
    round_trip_fee: Optional[float],
) -> RiskReport:
    reasons: List[str] = []
    executable = True

    # gate 1：行情时效（P1 验收：行情过期 → 禁止可执行开仓建议）
    if not quote_ok:
        executable = False
        reasons.append("行情过期/不可用（quote_ok=False），禁止生成可执行开仓建议")

    # gate 2：规则与输入完备性
    price_ok = _positive(price)
    atr_ok = _positive(atr)
    if spec is None:
        executable = False
        reasons.append("规则缺失：品种规格不在首期范围，无乘数/费用口径")
    if not price_ok:
        executable = False
        reasons.append(f"价格缺失或非正（price={price!r}），无法测算")
    if not atr_ok:
        executable = False
        reasons.append(f"ATR 缺失或非正（atr={atr!r}），无法计算止损距离")

    multiple_ok = _positive(params.atr_stop_multiple)
    slip_ok = _finite(params.slippage_ticks) and params.slippage_ticks >= 0  # type: ignore[operator]
    fee_value: Optional[float] = (
        spec.round_trip_fee if (spec is not None and round_trip_fee is None) else round_trip_fee
    )
    fee_ok = _finite(fee_value) and fee_value >= 0  # type: ignore[operator]
    funds_ok = _finite(params.risk_budget) and _finite(params.available_cash)

    # 会低估风险的非法参数：拒绝而非美化（见模块 docstring）
    if not multiple_ok:
        executable = False
        reasons.append(
            f"止损倍数非法（atr_stop_multiple={params.atr_stop_multiple!r}，需 > 0）"
        )
    if not slip_ok:
        executable = False
        reasons.append(
            f"滑点跳数非法（slippage_ticks={params.slippage_ticks!r}，需 ≥ 0）"
        )
    if not fee_ok:
        executable = False
        reasons.append(f"往返手续费非法（取值 {fee_value!r}，需 ≥ 0）")
    if not funds_ok:
        executable = False
        reasons.append(
            "资金参数非法（risk_budget="
            f"{params.risk_budget!r}, available_cash={params.available_cash!r}，需为有限数值）"
        )

    # —— 可算的部分先算：报告尽量给出数字，算不出的项保持 0 并已记录原因 ——
    safe_price = float(price) if price_ok else 0.0
    notional_value = notional(spec, safe_price) if (spec is not None and price_ok) else 0.0

    margin_value = 0.0
    margin_is_estimate = False
    if spec is not None and price_ok:
        if margin_per_hand is not None and _positive(margin_per_hand):
            margin_value = float(margin_per_hand)  # 快照值优先
        else:
            if margin_per_hand is not None:
                reasons.append(
                    f"快照保证金非法（margin_per_hand={margin_per_hand!r}），回退近似估算"
                )
            margin_value = margin_estimate(spec, safe_price)
            margin_is_estimate = True

    stop_distance = (
        float(atr) * float(params.atr_stop_multiple) if (atr_ok and multiple_ok) else 0.0
    )
    stop_distance_pct = (
        stop_distance / safe_price * 100.0 if (safe_price > 0.0 and stop_distance > 0.0) else 0.0
    )

    core_ok = spec is not None and price_ok and atr_ok and multiple_ok and slip_ok and fee_ok
    per_hand_risk_value = (
        per_hand_risk(spec, stop_distance, float(fee_value), float(params.slippage_ticks))
        if core_ok
        else 0.0
    )

    lots = 0
    if core_ok and funds_ok:
        lots = affordable_lots(
            per_hand_risk_value,
            float(params.risk_budget),
            float(params.available_cash),
            margin_value,
            float(fee_value),
        )
        # gate 3：最小一手开不起（P1 验收原文），reasons 必须带具体数字
        if lots == 0:
            executable = False
            per_hand_cash = margin_value + float(fee_value)
            binding = False
            if params.risk_budget < per_hand_risk_value:
                reasons.append(
                    f"最小一手风险 {per_hand_risk_value:.1f} 元"
                    f" > 单笔风险预算 {params.risk_budget:.1f} 元"
                )
                binding = True
            if params.available_cash < per_hand_cash:
                reasons.append(
                    f"每手占用 {per_hand_cash:.1f} 元"
                    f"（保证金 {margin_value:.1f} + 往返费用 {float(fee_value):.1f}）"
                    f" > 可用资金 {params.available_cash:.1f} 元"
                )
                binding = True
            if not binding:  # 兜底：保证 lots=0 必有带数字的解释
                reasons.append(
                    f"按每手风险 {per_hand_risk_value:.1f} 元与每手占用 {per_hand_cash:.1f} 元"
                    " 无法开出一手"
                )
        if params.max_lots is not None and params.max_lots > 0:
            lots = min(lots, int(params.max_lots))  # 管理性封顶，不改变可执行性
        elif params.max_lots is not None:
            executable = False
            lots = 0
            reasons.append(f"手数上限非法（max_lots={params.max_lots!r}，需为正整数）")

    # 近似保证金只提醒、不拒绝：三情形 gate 之外不阻断（见模块 docstring）
    if margin_is_estimate:
        reasons.append(
            f"保证金为近似估算（缺省率 {spec.margin_rate:.0%}），非期货公司实时快照"  # type: ignore[union-attr]
        )

    return RiskReport(
        symbol=str(symbol),
        price=safe_price,
        price_time=str(price_time),
        notional_per_hand=notional_value,
        margin_per_hand=margin_value,
        margin_is_estimate=margin_is_estimate,
        stop_distance=stop_distance,
        stop_distance_pct=stop_distance_pct,
        per_hand_risk=per_hand_risk_value,
        lots=lots,
        executable=executable,
        reasons=reasons,
        params=params,
        rule_version=spec.rule_version if spec is not None else "",
    )


def risk_report(
    spec: Optional[ProductSpec],
    *,
    symbol: str,
    price: Optional[float],
    price_time: str,
    atr: Optional[float],
    params: SizingParams,
    quote_ok: bool,
    margin_per_hand: Optional[float] = None,
    round_trip_fee: Optional[float] = None,
) -> RiskReport:
    """单品种风险测算入口（永不抛异常，非法输入落 reasons）。

    三情形 gate（P1 验收原文「行情过期、规则缺失或最小一手超过风险预算时，
    禁止生成可执行的开仓建议」）：

    1. ``quote_ok=False`` → executable=False，reasons 含「行情过期/不可用」；
    2. ``spec`` 缺失或 price/atr 缺失（None/≤0）→ executable=False，
       reasons 含「规则缺失」或对应字段缺失说明；
    3. ``affordable_lots(...) == 0`` → executable=False，reasons 给出具体数字
       （如「最小一手风险 686.2 元 > 单笔风险预算 500.0 元」）。

    其它约定：

    - ``stop_distance = atr × params.atr_stop_multiple``；每手风险按 §5 公式；
    - ``margin_per_hand`` 传入即用快照值（margin_is_estimate=False）；未传用
      spec 缺省率估算（True，reasons 注明「近似」但**不**因此拒绝 executable）；
    - ``round_trip_fee`` 未传用 spec.round_trip_fee；
    - 实现层异常（极端非法类型等）被捕获为 executable=False + reasons，保证
      调用方（监控循环）拿到的永远是一份可解释的报告。
    """
    try:
        return _risk_report_impl(
            spec,
            symbol=symbol,
            price=price,
            price_time=price_time,
            atr=atr,
            params=params,
            quote_ok=quote_ok,
            margin_per_hand=margin_per_hand,
            round_trip_fee=round_trip_fee,
        )
    except Exception as exc:  # noqa: BLE001 — 本函数职责就是解释"为什么不可执行"
        return RiskReport(
            symbol=str(symbol),
            price=float(price) if _positive(price) else 0.0,
            price_time=str(price_time),
            notional_per_hand=0.0,
            margin_per_hand=0.0,
            margin_is_estimate=False,
            stop_distance=0.0,
            stop_distance_pct=0.0,
            per_hand_risk=0.0,
            lots=0,
            executable=False,
            reasons=[f"输入非法，测算未完成：{exc!r}"],
            params=params,
            rule_version=spec.rule_version if isinstance(spec, ProductSpec) else "",
        )
