"""期货品种元数据（P0 产出，来源与核验记录见 docs/futures-p0-verification.md §3）。

手维护首期 5 品种（SHFE/DCE）的乘数与最小变动价位，来源=交易所合约规则页；
2026-09-19 经 ``futures_comm_info``「每跳毛利」列独立核验（rb/hc/m=10 元，i/cu=50 元，全对）。

保证金率 / 往返手续费是**期货公司快照口径的近似缺省**（comm_info 2026-09-18：
rb 2172.8 元/手 ÷ 名义 31040 ≈ 7% 等）。P1 风险测算优先用实时快照的每手保证金列，
缺省值仅在快照缺失时兜底，且结果必须带「近似」告警——不用未经验证的默认值掩盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

# 元数据版本：规格变更（乘数/夜盘/缺省费率）时更新，随信号与风险结果一起透出
RULE_VERSION = "20260919-p0"


@dataclass(frozen=True)
class ProductSpec:
    """品种级合约规格（乘数/最小变动/夜盘时段）。到期日是合约级信息，不入本表。"""

    code: str              # 品种代码，如 "RB"
    exchange: str          # "SHFE" / "DCE"
    name: str              # 展示名，如 "螺纹钢"
    multiplier: float      # 交易单位（吨/手 等）
    tick_size: float       # 最小变动价位（元/吨 等）
    trade_unit: str        # 展示用，如 "10吨/手"
    night_start: str       # 夜盘开市 "21:00"；无夜盘为 ""
    night_end: str         # 夜盘收市 "23:00"；跨零点记次日时刻如 "01:00"
    night_end_next_day: bool  # 夜盘收市是否在次一自然日（cu 到 01:00）
    margin_rate: float     # 近似保证金率（期货公司快照口径兜底，非交易所法定值）
    round_trip_fee: float  # 近似往返手续费 元/手（同上）
    rule_version: str = RULE_VERSION
    rule_source: str = "交易所合约规则页；futures_comm_info 2026-09-18 交叉核验"


#: 首期范围（P0 §1）：郑商所因新浪源空响应暂不可用，扩池前不改本表结构只加行
PRODUCT_SPECS: Dict[str, ProductSpec] = {
    "RB": ProductSpec("RB", "SHFE", "螺纹钢", 10.0, 1.0, "10吨/手",
                      "21:00", "23:00", False, 0.07, 6.2),
    "HC": ProductSpec("HC", "SHFE", "热轧卷板", 10.0, 1.0, "10吨/手",
                      "21:00", "23:00", False, 0.07, 6.6),
    "CU": ProductSpec("CU", "SHFE", "沪铜", 5.0, 10.0, "5吨/手",
                      "21:00", "01:00", True, 0.11, 164.2),
    "I": ProductSpec("I", "DCE", "铁矿石", 100.0, 0.5, "100吨/手",
                     "21:00", "23:00", False, 0.08, 28.6),
    "M": ProductSpec("M", "DCE", "豆粕", 10.0, 1.0, "10吨/手",
                     "21:00", "23:00", False, 0.07, 3.0),
}

# cu 快照核对：60241.5 / (109530×5) = 0.1100；i：5712 / (714×100) = 0.0800（P0 记录）
_SYMBOL_RE = re.compile(r"^([A-Za-z]{1,2})(\d{3,4})$")


@dataclass(frozen=True)
class ContractIdentity:
    """合约标识：RB2610 ⇄ RB2610.SHFE（canonical），郑商所 3 位月份码（TA701）也能解析。"""

    symbol: str        # 不带交易所后缀，如 "RB2610"
    canonical: str     # 带交易所后缀，如 "RB2610.SHFE"（库内与 API 的唯一键）
    product: str       # "RB"
    exchange: str      # "SHFE"
    year: int          # 2026
    month: int         # 10（1–12）


def product_spec(product: str) -> Optional[ProductSpec]:
    """品种代码 → ProductSpec；不在首期范围返回 None（调用方须显式降级，不得假造规格）。"""
    return PRODUCT_SPECS.get(str(product).upper())


def parse_contract_symbol(text: str) -> ContractIdentity:
    """解析合约代码 → ContractIdentity。

    4 位月份码（SHFE/DCE：RB2610 = 2026-10）与 3 位码（CZCE：TA701 = 2027-01）都支持；
    品种不在 PRODUCT_SPECS / 格式非法 → ValueError。
    """
    match = _SYMBOL_RE.match(str(text).strip().upper())
    if not match:
        raise ValueError(f"无法解析的合约代码：{text!r}（期望如 RB2610 / TA701）")
    product, digits = match.group(1), match.group(2)
    spec = product_spec(product)
    if spec is None:
        raise ValueError(f"品种 {product} 不在首期范围（{sorted(PRODUCT_SPECS)}）")
    if len(digits) == 4:
        year, month = 2000 + int(digits[:2]), int(digits[2:])
    else:
        year, month = 2000 + int(digits[0]), int(digits[1:])
    if not 1 <= month <= 12:
        raise ValueError(f"合约月份非法：{text!r} → {month}")
    symbol = f"{product}{digits}"
    return ContractIdentity(symbol, f"{symbol}.{spec.exchange}", product,
                            spec.exchange, year, month)


def canonical_symbol(text: str) -> str:
    return parse_contract_symbol(text).canonical


def tick_value(spec: ProductSpec) -> float:
    """每跳盈亏 = 乘数 × 最小变动价位（P0 已与 comm_info「每跳毛利」列核对一致）。"""
    return spec.multiplier * spec.tick_size


def notional(spec: ProductSpec, price: float) -> float:
    """每手名义价值 = 价格 × 乘数。"""
    return float(price) * spec.multiplier


def margin_estimate(spec: ProductSpec, price: float, margin_rate: Optional[float] = None) -> float:
    """每手保证金估计。``margin_rate`` 缺省用快照近似率——调用方须透出「近似」口径。"""
    rate = spec.margin_rate if margin_rate is None else float(margin_rate)
    return notional(spec, price) * rate


def products_by_exchange() -> Dict[str, list]:
    return {
        exchange: sorted(spec.code for spec in PRODUCT_SPECS.values()
                         if spec.exchange == exchange)
        for exchange in sorted({spec.exchange for spec in PRODUCT_SPECS.values()})
    }
