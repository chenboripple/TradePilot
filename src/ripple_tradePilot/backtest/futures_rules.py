"""期货费用与保证金规则层（roadmap P2 第一块：撮合层与账户层的成本口径单一来源）。

对接 docs/futures-roadmap.md P2「实现按手或按成交额收费，支持开仓、平今、平昨
差异及规则按日期生效」与「加入保证金不足、保证金调整处理」。接口已钉死，撮合层
（按 offset 逐笔取费）与账户层（逐日重算冻结保证金）按本模块签名对接，勿改字段名。

三条铁律（WHY 它们长这样）：

1. **规则缺失 → 禁止成交**（P1 已定的 gate 哲学）：``rule_on``/``rate_on`` 在
   缺口日期返回 None，``fee_for`` 在该 offset 档缺价时返回 None，由撮合层拦截
   该笔成交——绝不用 0 或猜测值填补。漏算成本的成交会虚增可成交信号并把错账
   写进权益曲线，危害远大于少成交一笔。
2. **区间两端都含当日**：交易所公告口径是「自 X 日起（含当日）按新标准收取」；
   effective_from 当日新规则即生效、effective_to 当日旧规则仍生效、次日切换。
   区间重叠 = 同一天两个价，构造期 ValueError 拒绝，绝不静默取先/取后。
3. **近似口径必须显式标记**（P0 遗留要求：历史回测缺官方逐日规则时标注近似）：
   用期货公司快照等近似值填的规则必须置 ``approximate=True``，
   ``is_approximate_on`` 把标记透出到账单与报告，禁止近似值冒充官方口径。

纯离线模块：不联网、不落库，只依赖 stdlib 与 ``models.types.FuturesOffset``。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional, Tuple

from ripple_tradePilot.models.types import FuturesOffset


def _require_date_code(code: str, field: str) -> None:
    """日期字段硬校验：必须为零填充 8 位 YYYYMMDD 且为真实日历日。

    WHY 在构造期就把格式钉死：本模块所有区间比较（重叠检测、bisect 查找）都
    用**字符串字典序**等价时间序——这一等价只对零填充 8 位数字成立。放进一个
    "2025-1-5" 之类的杂格式，重叠检测会静默得出错误结论，比直接报错危险得多。
    """
    # 类型必须就是 str：int 20250101 经 str() 也能转成合法日期码，但混进
    # Schedule 后与 str 键排序/比较会抛 TypeError——错误越早暴露越好。
    if not isinstance(code, str):
        raise ValueError(f"{field} 须为 YYYYMMDD 字符串，得到 {type(code).__name__}：{code!r}")
    text = code
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"{field} 须为 YYYYMMDD 8 位数字，得到 {code!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        raise ValueError(f"{field} 非法日历日期：{code!r}") from None


def _is_positive_finite(value: Any) -> bool:
    """有限且 > 0。NaN 与任何数比较都是 False，必须用 isfinite 显式挡掉，
    否则 NaN 会一路静默传染进账本。"""
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class FeeRule:
    """一个日期区间内的手续费规则：开/平今/平昨三档独立，每档按手或按成交额二选一。

    approximate=True 表示该区间费率是近似口径（如期货公司快照推导），回测结果
    须随账单透出「近似」告警，不得冒充交易所官方规则。
    """

    effective_from: str            # "YYYYMMDD"，含当日
    effective_to: Optional[str]    # None = 开区间；含当日
    approximate: bool = False      # 近似口径标记（历史回测缺官方规则时 True）
    open_per_lot: Optional[float] = None       # 按手：元/手
    open_by_ratio: Optional[float] = None      # 按成交额：占成交额的小数比（万1 传 0.0001）
    close_today_per_lot: Optional[float] = None
    close_today_by_ratio: Optional[float] = None
    close_yesterday_per_lot: Optional[float] = None
    close_yesterday_by_ratio: Optional[float] = None

    def __post_init__(self) -> None:
        _require_date_code(self.effective_from, "effective_from")
        if self.effective_to is not None:
            _require_date_code(self.effective_to, "effective_to")
            # 倒挂区间永远匹配不到任何日期，等于静默挖坑，构造期即拒绝
            if self.effective_to < self.effective_from:
                raise ValueError(
                    f"effective_to {self.effective_to} 早于 effective_from "
                    f"{self.effective_from}：{self!r}")
        covered = False
        for per_lot_name, by_ratio_name in (
            ("open_per_lot", "open_by_ratio"),
            ("close_today_per_lot", "close_today_by_ratio"),
            ("close_yesterday_per_lot", "close_yesterday_by_ratio"),
        ):
            per_lot = getattr(self, per_lot_name)
            by_ratio = getattr(self, by_ratio_name)
            if per_lot is not None and by_ratio is not None:
                # 同档双设意味着「按手还是按成交额」无法判定，两个调用方会算出
                # 两套账——构造数据错误必须早暴露，不静默取其一
                raise ValueError(
                    f"{per_lot_name} 与 {by_ratio_name} 同时设置（按手/按成交额"
                    f"只能二选一）：{self!r}")
            for name, value in ((per_lot_name, per_lot), (by_ratio_name, by_ratio)):
                # 负费率 = 交易所倒贴成交，现实中不存在；NaN 会静默传染账本。
                # 0 合法（品种阶段性免收手续费）。
                if value is not None and not (math.isfinite(value) and value >= 0):
                    raise ValueError(f"{name} 非法：{value!r}（须为有限非负数）")
            if per_lot is not None or by_ratio is not None:
                covered = True
        if not covered:
            # 三档全缺的规则不可能产生任何合法费用，构造它本身就是 bug
            raise ValueError(f"FeeRule 三档全缺（空规则无意义）：{self!r}")


class _IntervalSchedule:
    """闭区间规则的共享容器：构造期查重叠 + 按日期二分查找。

    子类（FeeSchedule/MarginSchedule）的规则项都有 effective_from/effective_to
    属性；区间语义统一为 [from, to] 两端含当日，to=None 开区间。
    """

    def __init__(self, rules: List[Any]) -> None:
        # 构造期排序：调用方无须保证传入顺序；重叠检测与查找都依赖有序性
        items = sorted(rules, key=lambda rule: rule.effective_from)
        for prev, nxt in zip(items, items[1:]):
            # 前一区间开区间（to=None）必然吞掉后续一切；否则两闭区间在
            # nxt.from <= prev.to 时共享至少一天（两端都含当日）
            if prev.effective_to is None or nxt.effective_from <= prev.effective_to:
                raise ValueError(
                    f"规则区间重叠（同一天将有两个价）：{prev!r} vs {nxt!r}")
        self._items = items
        self._starts = [rule.effective_from for rule in items]

    def _item_on(self, trade_date: str) -> Optional[Any]:
        """交易日 → 生效规则；缺口日期返回 None（gate，见模块 docstring 铁律 1）。

        查找日期同样走 YYYYMMDD 硬校验：杂格式日期静默查错规则比报错危险。
        """
        _require_date_code(trade_date, "trade_date")
        # 二分：最后一个 from <= trade_date 的候选。区间互不重叠（构造期已保证），
        # 所以候选至多一个能覆盖该日；候选不覆盖即为缺口，返回 None。
        pos = bisect_right(self._starts, trade_date) - 1
        if pos < 0:
            return None
        item = self._items[pos]
        if item.effective_to is None or trade_date <= item.effective_to:
            return item
        return None


class FeeSchedule(_IntervalSchedule):
    """某品种手续费规则的日期轴序列：互不重叠的 FeeRule 闭区间。"""

    def __init__(self, rules: List[FeeRule]):  # 区间重叠 → ValueError（构造期拒绝，绝不静默）
        # WHY 构造期拒绝而非查表时报警：重叠是装配数据错误，带病跑完整个回测
        # 才发现两个区间撞车，排查成本远高于启动时炸掉。
        super().__init__(rules)

    def rule_on(self, trade_date: str) -> Optional[FeeRule]:  # 无覆盖区间 → None
        return self._item_on(trade_date)

    def is_approximate_on(self, trade_date: str) -> bool:  # None 视为 False
        # WHY 缺规则 → False 而不是 True：近似标记只描述「已生效规则的口径来源」；
        # 无规则日期的告警由 rule_on is None 这条 gate 负责，两种语义不混用。
        rule = self.rule_on(trade_date)
        return bool(rule.approximate) if rule is not None else False


def _offset_tier(rule: FeeRule, offset: FuturesOffset) -> Tuple[Optional[float], Optional[float]]:
    """offset → (per_lot, by_ratio) 档位值。CLOSE 映射到平昨档（与引擎 plain CLOSE
    的「昨仓优先」拆分规则口径一致）；能拆今昨时撮合层应显式传 CLOSE_TODAY/CLOSE_YESTERDAY。"""
    if offset is FuturesOffset.OPEN:
        return rule.open_per_lot, rule.open_by_ratio
    if offset is FuturesOffset.CLOSE_TODAY:
        return rule.close_today_per_lot, rule.close_today_by_ratio
    if offset in (FuturesOffset.CLOSE, FuturesOffset.CLOSE_YESTERDAY):
        return rule.close_yesterday_per_lot, rule.close_yesterday_by_ratio
    raise ValueError(f"未知 FuturesOffset：{offset!r}")


def fee_for(rule: FeeRule, offset: FuturesOffset, price: float, lots: int,
            multiplier: float) -> Optional[float]:
    """按 offset 查对应档：CLOSE 映射到 close_yesterday 档（保守取高口径；撮合层会显式传
    CLOSE_TODAY/CLOSE_YESTERDAY，只有无法拆分时才传 CLOSE）。
    该 offset 档位缺（by_lot 与 by_ratio 都为 None）→ None（规则缺失 gate）。
    同一 offset 两个字段同时设置 → ValueError（构造数据错误，早暴露）。
    按手 = per_lot × lots；按成交额 = ratio × price × multiplier × lots。
    数值垃圾（price/lots/multiplier ≤ 0 或 NaN）→ None（与 margin_for 抛 ValueError
    的钉死分工：费用算不出＝这笔成交可疑，交撮合层 gate 掉，不炸整个回测）。"""
    if not _is_positive_finite(price) or not _is_positive_finite(multiplier):
        return None
    # lots 声明为 int，仍按 float 校验以挡掉 float("nan") 之类的漏网垃圾
    if not _is_positive_finite(lots):
        return None
    per_lot, by_ratio = _offset_tier(rule, offset)
    if per_lot is not None and by_ratio is not None:
        # 防御性复检：__post_init__ 已在构造期拦截同档双设，这里只挡
        # 绕过 dataclass 校验的极端用法与未来重构引入的回归
        raise ValueError(f"offset {offset} 同档双设（按手/按成交额只能二选一）：{rule!r}")
    if per_lot is None and by_ratio is None:
        # 规则缺失 gate：该档没有定价 → 返回 None 由撮合层禁止成交，
        # 用 0 填会把不该成交的信号放进来
        return None
    if per_lot is not None:
        return float(per_lot * lots)
    return float(by_ratio * price * multiplier * lots)


@dataclass(frozen=True)
class MarginRule:
    """一个日期区间内的保证金率（交易所 + 期货公司合计口径）。

    rate 合法域 (0, 1]：0/负值意味着免保证金（不存在），>1 意味着保证金比名义
    价值还多（数据错误）。approximate=True 同 FeeRule，用于快照等近似口径。
    """

    effective_from: str
    effective_to: Optional[str]
    rate: float
    approximate: bool = False

    def __post_init__(self) -> None:
        _require_date_code(self.effective_from, "effective_from")
        if self.effective_to is not None:
            _require_date_code(self.effective_to, "effective_to")
            if self.effective_to < self.effective_from:
                raise ValueError(
                    f"effective_to {self.effective_to} 早于 effective_from "
                    f"{self.effective_from}：{self!r}")
        # NaN 与任何比较都是 False，不显式 isfinite 会静默穿过 0 < rate <= 1
        if not (math.isfinite(self.rate) and 0 < self.rate <= 1):
            raise ValueError(
                f"保证金率非法：{self.rate!r}（须为 (0, 1] 内的有限数，"
                f"交易所+期货公司合计口径不会越界）")


class MarginSchedule(_IntervalSchedule):
    """某品种保证金率规则的日期轴序列：互不重叠的 MarginRule 闭区间。"""

    def __init__(self, rules: List[MarginRule]):  # 重叠 → ValueError
        super().__init__(rules)

    def rate_on(self, trade_date: str) -> Optional[float]:  # 缺口 → None
        rule = self._item_on(trade_date)
        return None if rule is None else float(rule.rate)

    def is_approximate_on(self, trade_date: str) -> bool:
        rule = self._item_on(trade_date)
        return bool(rule.approximate) if rule is not None else False


def margin_for(rate: float, price: float, lots: int, multiplier: float) -> float:
    """price × multiplier × lots × rate。

    数值垃圾（rate/price/lots/multiplier ≤ 0 或 NaN）→ ValueError（与 fee_for 返回
    None 的钉死分工）：账本层依赖保证金一定可算——调用前已过 rate_on 的 None 检查，
    这里还算不出说明是编程错误，直接炸掉暴露，绝不让 None/NaN 流进权益曲线。"""
    if not _is_positive_finite(rate):
        raise ValueError(f"rate 非法：{rate!r}（须为有限正数，合法域由 MarginRule 保证）")
    if not _is_positive_finite(price) or not _is_positive_finite(multiplier):
        raise ValueError(f"price/multiplier 非法：price={price!r}, multiplier={multiplier!r}")
    if not _is_positive_finite(lots):
        raise ValueError(f"lots 非法：{lots!r}（须为正数手数）")
    return float(price) * float(multiplier) * lots * float(rate)
