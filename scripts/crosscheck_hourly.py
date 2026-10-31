"""把查询系统和另一个行情源的同一根小时线逐点对照，输出可直接交付的核验表。

参考数据用 CSV 提供，格式三列：``ticker,bar_end,close``。
``bar_end`` 是这根小时线的**结束时刻**（带时区，如 2026-10-22T10:30:00+08:00）。
来源不限——Yahoo 导出、东方财富、雪球、或人工从网页抄下来的值都行。
这样仓库里不用写死任何一家供应商的抓取逻辑，换源只要换 csv。

用法::

    python scripts/crosscheck_hourly.py --db prices.sqlite3 \\
        --point 0883.HK=2026-10-22T10:31:00+08:00 \\
        --point XOM=2026-10-28T11:31:00-04:00 \\
        --reference reference.csv --format table

``--point`` 给的是**新闻发布时间**（bar 结束后再过一分钟），系统会取该时刻前
最后一根已完成的小时线。输出逐点列出系统时间/价格、参考价格、差值和判定；
任一不一致时退出码为 1，可直接接 CI。
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_price_query.assets import resolve_asset  # noqa: E402
from news_price_query.price_store import LocalDbPriceProvider, PriceStore  # noqa: E402
from news_price_query.query import query_event  # noqa: E402


def read_reference(path: str) -> dict[tuple[str, str], float]:
    """读参考 CSV；bar_end 先归一化成带时区的 ISO，避免格式差异导致匹配不上。"""
    reference: dict[tuple[str, str], float] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip()
            stamp = (row.get("bar_end") or "").strip()
            close = (row.get("close") or "").strip()
            if not (ticker and stamp and close):
                continue
            reference[(ticker, datetime.fromisoformat(stamp).isoformat())] = float(close)
    return reference


def compare(provider, points, reference, as_of=None, tolerance=1e-4):
    """逐点比对，返回结果行；不联网、不写文件，方便单测。"""
    rows = []
    for ticker, event in points:
        asset = resolve_asset(ticker)
        result = query_event(provider, asset, event, as_of=as_of, mode="hourly")
        base = result.baseline
        sys_end = base.observed_at.isoformat() if base.observed_at else None
        ref_close = reference.get((asset.ticker, sys_end)) if sys_end else None
        row = {
            "ticker": asset.ticker,
            "input": event.isoformat(),
            "system_bar_end": sys_end,
            "system_close": base.price,
            "reference_close": ref_close,
            "diff": None,
            "flag": result.baseline_flag,
            "verdict": "缺参考值",
        }
        if base.price is None:
            row["verdict"] = "系统缺数据"
        elif ref_close is not None:
            # 先取整到 6 位再比，避免 23.04-23.05 这类浮点尾巴把“恰好等于容差”判成超出
            diff = round(base.price - ref_close, 6)
            row["diff"] = diff
            row["verdict"] = "一致" if abs(diff) <= tolerance else "不一致"
        rows.append(row)
    return rows


def parse_point(text: str):
    # 期货代码本身带等号（BZ=F），所以从右边切，别把 ticker 切断
    try:
        ticker, stamp = text.rsplit("=", 1)
    except ValueError:
        raise argparse.ArgumentTypeError("--point 需要 TICKER=ISO时间，例如 0883.HK=2026-10-22T10:31:00+08:00") from None
    moment = datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        raise argparse.ArgumentTypeError("--point 的时间必须带时区，例如 +08:00 或 -04:00")
    return ticker.strip(), moment


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crosscheck_hourly", description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="本地价格库路径")
    parser.add_argument("--point", required=True, action="append", type=parse_point,
                        help="TICKER=新闻发布时间(带时区)，可重复")
    parser.add_argument("--reference", required=True, help="参考 CSV：ticker,bar_end,close")
    parser.add_argument("--as-of", help="可复现的截止时刻，ISO 8601 带时区；默认现在")
    parser.add_argument("--tolerance", type=float, default=1e-4, help="允许的绝对误差，默认 0.0001")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)

    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    provider = LocalDbPriceProvider(store=PriceStore(args.db, create=False))
    rows = compare(provider, args.point, read_reference(args.reference), as_of, args.tolerance)

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(f"{'标的':9s}{'系统 bar 结束':26s}{'系统价':>12s}{'参考价':>12s}{'差':>10s}  判定")
        for row in rows:
            system_close = "—" if row["system_close"] is None else f"{row['system_close']:.4f}"
            reference_close = "—" if row["reference_close"] is None else f"{row['reference_close']:.4f}"
            diff = "—" if row["diff"] is None else f"{row['diff']:+.4f}"
            print(f"{row['ticker']:9s}{(row['system_bar_end'] or '—')[:25]:26s}"
                  f"{system_close:>12s}{reference_close:>12s}{diff:>10s}  {row['verdict']}")
        matched = sum(row["verdict"] == "一致" for row in rows)
        print(f"\n一致 {matched}/{len(rows)}")
    return 0 if all(row["verdict"] == "一致" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
