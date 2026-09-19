#!/usr/bin/env python3
"""P0 数据源探针：验证 futures-roadmap.md §7 第 1 周清单第 2-4 项。

逐项回答（结论写入 docs/futures-p0-verification.md，本脚本可复现）：
  1. 合约元数据：交易所合约表能否拿到 交割月份/乘数/最小变动价位？
  2. 两个交割月份样例：真实合约日线（含持仓量）的时间戳/列名/条数/缺失？
  3. 60 分钟线：夜盘时段是否存在、时间戳口径、归属交易日如何推断？
  4. 主力映射：新浪主力表 + 成交量/持仓量判主力的口径。
  5. 结算价/涨跌停：交易所每日行情表（futures_settle_*）能否补齐。

用法：python3 experiments/futures/p0_probe.py [--json OUT]
网络失败不抛栈：每项独立 try/except，输出 OK/FAIL 摘要。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import akshare as ak  # noqa: E402

FIELDS = ("shape", "columns")


def _brief(df, n=2, tail=1):
    """DataFrame → 摘要 dict（shape/columns/头尾几行），避免整表刷屏。"""
    if df is None:
        return None
    info = {"shape": list(df.shape), "columns": [str(c) for c in df.columns]}
    info["head"] = df.head(n).astype(str).to_dict(orient="records")
    info["tail"] = df.tail(tail).astype(str).to_dict(orient="records")
    return info


def probe(name):
    """装饰器：登记探针函数，异常只记不炸。"""
    def wrap(fn):
        PROBES.append((name, fn))
        return fn
    return wrap


PROBES = []


@probe("shfe_contract_info: 上交所? No—上期所合约表（品种/合约/交割月）")
def _p1():
    df = ak.futures_contract_info_shfe()
    rb = df[df["PRODUCT"].str.upper() == "RB"] if "PRODUCT" in df.columns else df.head(0)
    return {"all_shape": list(df.shape), "rb_count": len(rb),
            "rb_head": rb.head(8).astype(str).to_dict(orient="records"),
            "columns": [str(c) for c in df.columns]}


@probe("dce_contract_info: 大商所合约表（跨所样例，取 I/M）")
def _p2():
    df = ak.futures_contract_info_dce()
    hit = df[df["品种"].str.contains("铁矿石|豆粕", na=False)] if "品种" in df.columns else df.head(0)
    return {"all_shape": list(df.shape), "hit_count": len(hit),
            "hit_head": hit.head(8).astype(str).to_dict(orient="records"),
            "columns": [str(c) for c in df.columns]}


@probe("czce_contract_info: 郑商所合约表（跨所样例，验证郑商所 3 位月份码）")
def _p3():
    df = ak.futures_contract_info_czce()
    hit = df[df["品种"].str.contains("TA|甲醇", na=False)] if "品种" in df.columns else df.head(0)
    return {"all_shape": list(df.shape), "hit_count": len(hit),
            "hit_head": hit.head(8).astype(str).to_dict(orient="records"),
            "columns": [str(c) for c in df.columns]}


@probe("sina_daily_RB2610: 新浪日线（真实合约，含持仓量）")
def _p4():
    df = ak.futures_zh_daily_sina(symbol="RB2610")
    return _brief(df, n=3, tail=2)


@probe("sina_daily_RB2701: 新浪日线（第二个交割月）")
def _p5():
    df = ak.futures_zh_daily_sina(symbol="RB2701")
    return _brief(df, n=3, tail=2)


@probe("sina_daily_TA701: 郑商所品种新浪日线（验证 3 位月份码口径）")
def _p6():
    df = ak.futures_zh_daily_sina(symbol="TA701")
    return _brief(df, n=3, tail=2)


@probe("sina_minute60_RB2610: 新浪 60 分钟线（夜盘时段核验）")
def _p7():
    df = ak.futures_zh_minute_sina(symbol="RB2610", period="60")
    # 夜盘证据：21:00 及以后的 bar（螺纹钢夜盘 21:00-23:00）
    times = df["datetime"].astype(str)
    night = df[times.str.contains(" 21:| 22:| 23:")]
    return {**_brief(df, n=3, tail=2), "night_bar_count": len(night),
            "night_tail": night.tail(3).astype(str).to_dict(orient="records")}


@probe("sina_main_display: 新浪主力行情表（主力映射口径）")
def _p8():
    df = ak.futures_display_main_sina()
    rb = df[df["symbol"].str.contains("^RB", regex=True, na=False)]
    return {"columns": [str(c) for c in df.columns], "rb_rows":
            rb.astype(str).to_dict(orient="records")}


@probe("shfe_settle_20260918: 上期所每日行情（结算价/涨跌停口径）")
def _p9():
    df = ak.futures_settle_shfe("20260918")
    rb = df[df.iloc[:, 0].astype(str).str.contains("rb", case=False, na=False)]
    return {"columns": [str(c) for c in df.columns], "rb_rows":
            rb.head(5).astype(str).to_dict(orient="records")}


@probe("comm_info: 品种基础信息（乘数/最小变动候选来源）")
def _p10():
    df = ak.futures_comm_info()
    return _brief(df, n=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args()

    results, ok, fail = {}, 0, 0
    for name, fn in PROBES:
        try:
            results[name] = fn()
            ok += 1
            print(f"OK   {name}")
        except Exception as error:  # 探针失败本身是结论（接口不可用），记录之
            results[name] = {"error": f"{type(error).__name__}: {error}"}
            fail += 1
            print(f"FAIL {name}: {type(error).__name__}: {error}")
        if args.json:
            Path(args.json).write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print(f"\n{ok} ok / {fail} fail；详细结果 {'→ ' + args.json if args.json else '未落盘(--json)'}")


if __name__ == "__main__":
    main()
