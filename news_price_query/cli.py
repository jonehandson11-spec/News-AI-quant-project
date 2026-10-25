"""Command-line interface for single and batch price lookup."""

import argparse
import csv
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .assets import resolve_asset
from .news import find_direct_match, read_news
from .prices import CsvPriceProvider, FutuPriceProvider
from .query import query_event
from .windows import TradingWindowConfig, window_label_zh


HK = ZoneInfo("Asia/Hong_Kong")


def _time(value: str, zone: str | None) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if zone is None:
            raise ValueError("Naive timestamp needs --timezone, for example Asia/Hong_Kong")
        parsed = parsed.replace(tzinfo=ZoneInfo(zone))
    return parsed.astimezone(HK)


def _provider(args):
    if args.provider == "local":
        from .price_store import LocalDbPriceProvider

        return LocalDbPriceProvider(args.db)
    if args.provider == "csv":
        if not args.prices:
            raise ValueError("--prices is required with --provider csv")
        return CsvPriceProvider(args.prices)
    return FutuPriceProvider(host=args.futu_host, port=args.futu_port)


def _days(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError(f"Window days must be comma-separated integers: {value}") from exc


def _query_options(args) -> dict:
    if args.mode == "hourly":
        return {"mode": "hourly"}
    if args.mode != "daily":
        return {}
    config = TradingWindowConfig(before_days=_days(args.before_days), after_days=_days(args.after_days))
    return {"mode": "daily", "config": config}


def _output_path(value: str) -> Path:
    path = Path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _result_dict(result, *, news=None, match=None, benchmark=None) -> dict:
    payload = {
        "ticker": result.asset.ticker,
        "asset_role": result.asset.role,
        "published_at": result.published_at.isoformat(),
        "baseline": {
            "observed_at": result.baseline.observed_at.isoformat(),
            "price": result.baseline.price,
            "source": result.baseline.source,
            "basis": result.baseline.basis,
            "gap_seconds": result.baseline_gap_seconds,
            "status": result.baseline_status,
        },
        "windows": {
            item.label: {
                "label_zh": window_label_zh(item.label),
                "target_at": item.target_at.isoformat() if item.target_at else None,
                "observed_at": item.observed_at.isoformat() if item.observed_at else None,
                "price": item.price,
                "return_pct": item.return_pct,
                "status": item.status,
                "delay_seconds": item.delay_seconds,
                "source": item.source,
                "basis": item.basis,
            }
            for item in result.windows
        },
    }
    if result.mode == "daily":
        payload["mode"] = "daily"
        payload["session"] = result.session
        payload["t0"] = result.t0.isoformat()
    if news:
        payload.update({
            "article_id": news.article_id, "title": news.title,
            "source": news.source, "url": news.url,
            "match": asdict(match),
        })
    if benchmark is not None:
        payload["benchmark_ticker"] = benchmark.asset.ticker
        payload["benchmark_baseline"] = {
            "observed_at": benchmark.baseline.observed_at.isoformat(),
            "price": benchmark.baseline.price,
            "source": benchmark.baseline.source,
            "basis": benchmark.baseline.basis,
            "gap_seconds": benchmark.baseline_gap_seconds,
            "status": benchmark.baseline_status,
        }
        baselines_aligned = (
            abs((result.baseline.observed_at - benchmark.baseline.observed_at).total_seconds()) <= 60
            and result.baseline.basis == benchmark.baseline.basis
        )
        comparison = {item.label: item for item in benchmark.windows}
        for item in result.windows:
            other = comparison[item.label]
            entry = payload["windows"][item.label]
            entry["benchmark_observed_at"] = other.observed_at.isoformat() if other.observed_at else None
            entry["benchmark_return_pct"] = other.return_pct
            aligned = (
                baselines_aligned and item.observed_at is not None and other.observed_at is not None
                and (item.observed_at.date() == other.observed_at.date() if item.label.startswith("before_")
                     else abs((item.observed_at - other.observed_at).total_seconds()) <= 60)
            )
            entry["excess_return_pct"] = (
                item.return_pct - other.return_pct
                if aligned and item.return_pct is not None and other.return_pct is not None else None
            )
    return payload


def _add_common(parser):
    parser.add_argument("--asset", required=True, help="Asset name or HK ticker")
    parser.add_argument("--provider", choices=("csv", "futu", "local"), default="local",
                        help="local = SQLite price store filled by scripts/update_prices.py")
    parser.add_argument("--db", help="Local price store path (default ~/.news_price_query/prices.sqlite3)")
    parser.add_argument("--prices", help="CSV with ticker,interval,timestamp,close")
    parser.add_argument("--as-of", help="Reproducible cutoff, ISO 8601 with offset")
    parser.add_argument("--timezone", help="Required for a manual time with no offset")
    parser.add_argument("--futu-host", default="127.0.0.1")
    parser.add_argument("--futu-port", type=int, default=11111)
    parser.add_argument("--benchmark", help="Optional benchmark ETF, such as 2800.HK")
    parser.add_argument("--mode", choices=("hourly", "minute", "daily"), default="hourly",
                        help="hourly: trading-hour windows; minute: legacy clock-time windows; daily: trading-day windows")
    parser.add_argument("--before-days", default="5,21,63,126",
                        help="Daily mode: trading days before the baseline day")
    parser.add_argument("--after-days", default="1,3,5",
                        help="Daily mode: t+K trading days after t0")


def _parser():
    parser = argparse.ArgumentParser(prog="news-price-query")
    commands = parser.add_subparsers(dest="command", required=True)
    single = commands.add_parser("query", help="Look up one asset and news time")
    _add_common(single)
    single.add_argument("--time", required=True, help="Publication time in ISO 8601")
    single.add_argument("--format", choices=("json", "table"), default="json",
                        help="table: start/end date+price per window (daily local mode)")
    batch = commands.add_parser("batch", help="Query articles with a direct asset mention")
    _add_common(batch)
    batch.add_argument("--news", required=True, help="Team news.csv or BBC export")
    batch.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    batch.add_argument("--output", help="Write results here instead of stdout")
    scan = commands.add_parser("scan", help="List direct-mention news without market data")
    scan.add_argument("--asset", required=True)
    scan.add_argument("--news", required=True)
    scan.add_argument("--output", help="Write JSON report here instead of stdout")
    study = commands.add_parser("study", help="Summarize batch JSONL returns and coverage")
    study.add_argument("--input", required=True, help="Batch JSONL file")
    return parser


_CSV_COLUMNS = (
    "article_id", "source", "title", "url", "ticker", "mode", "price_unit", "currency", "match_type", "match_evidence",
    "published_at", "baseline_at", "baseline_price", "baseline_adj_price", "baseline_gap_seconds",
    "baseline_status", "window", "target_at",
    "window_label_zh", "observed_at", "price", "adj_price", "start_at", "start_price", "end_at", "end_price",
    "return_pct", "adj_return_pct", "price_change", "status", "flag", "delay_seconds",
    "price_source", "source_detail", "price_basis", "benchmark_ticker", "benchmark_baseline_at",
    "benchmark_baseline_price", "benchmark_baseline_status", "benchmark_return_pct",
    "excess_return_pct", "adj_excess_return_pct", "benchmarks_json", "error",
)


def _write_csv(rows: list[dict], handle):
    writer = csv.DictWriter(handle, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for item in rows:
        common = {
            "article_id": item.get("article_id"), "source": item.get("source"),
            "title": item.get("title"), "url": item.get("url"),
            "ticker": item.get("ticker"),
            "mode": item.get("mode"), "price_unit": item.get("price_unit"), "currency": item.get("currency"),
            "match_type": (item.get("match") or {}).get("kind"),
            "match_evidence": (item.get("match") or {}).get("evidence"),
            "published_at": item.get("published_at"),
            "baseline_at": (item.get("baseline") or {}).get("observed_at"),
            "baseline_price": (item.get("baseline") or {}).get("price"),
            "baseline_adj_price": (item.get("baseline") or {}).get("adj_price"),
            "baseline_gap_seconds": (item.get("baseline") or {}).get("gap_seconds"),
            "baseline_status": (item.get("baseline") or {}).get("status"),
            "benchmark_ticker": item.get("benchmark_ticker"),
            "benchmark_baseline_at": (item.get("benchmark_baseline") or {}).get("observed_at"),
            "benchmark_baseline_price": (item.get("benchmark_baseline") or {}).get("price"),
            "benchmark_baseline_status": (item.get("benchmark_baseline") or {}).get("status"),
            "error": item.get("error"),
        }
        windows = item.get("windows") or {"": {"status": item.get("status")}}
        for label, window in windows.items():
            writer.writerow({
                **common, "window": label, "target_at": window.get("target_at"),
                "window_label_zh": window.get("label_zh"), "start_at": window.get("start_at"),
                "start_price": window.get("start_price"), "end_at": window.get("end_at"),
                "end_price": window.get("end_price"), "adj_price": window.get("adj_price"),
                "observed_at": window.get("observed_at"), "price": window.get("price"),
                "return_pct": window.get("return_pct"), "adj_return_pct": window.get("adj_return_pct"),
                "price_change": window.get("price_change"), "flag": window.get("flag"), "status": window.get("status"),
                "delay_seconds": window.get("delay_seconds"),
                "price_source": window.get("source"), "source_detail": window.get("source_detail"), "price_basis": window.get("basis"),
                "benchmark_return_pct": window.get("benchmark_return_pct"),
                "excess_return_pct": window.get("excess_return_pct"),
                "adj_excess_return_pct": window.get("adj_excess_return_pct"),
                "benchmarks_json": json.dumps({code: (benchmark.get("windows") or {}).get(label)
                    for code, benchmark in (item.get("benchmarks") or {}).items()}, ensure_ascii=False),
            })


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    provider = None
    try:
        if args.command == "study":
            from .study import summarize

            with Path(args.input).open("r", encoding="utf-8-sig") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
            return 0
        asset = resolve_asset(args.asset)
        if args.command == "scan":
            articles = read_news(args.news)
            matches = []
            for item in articles:
                match = find_direct_match(item, asset)
                if match is not None:
                    matches.append({
                        "article_id": item.article_id, "source": item.source,
                        "title": item.title, "published_at": item.published_at.isoformat(),
                        "url": item.url, "match_type": match.kind,
                        "evidence": match.evidence, "verified_by_human": False,
                    })
            report = {
                "ticker": asset.ticker, "articles_total": len(articles),
                "matches_total": len(matches),
                "title_mentions": sum(row["match_type"] == "title_mention" for row in matches),
                "body_mentions": sum(row["match_type"] == "body_mention" for row in matches),
                "matches": matches,
            }
            text = json.dumps(report, ensure_ascii=False, indent=2)
            if args.output:
                _output_path(args.output).write_text(text + "\n", encoding="utf-8")
            else:
                print(text)
            return 0
        as_of = _time(args.as_of, args.timezone) if args.as_of else datetime.now(HK)
        event_time = _time(args.time, args.timezone) if args.command == "query" else None
        benchmark_asset = resolve_asset(args.benchmark) if args.benchmark else None
        if args.provider == "local" and args.mode == "minute":
            raise ValueError("--provider local has no minute bars; select hourly or daily")
        if benchmark_asset is not None and benchmark_asset.role != "benchmark":
            raise ValueError("--benchmark must be a registered benchmark ETF")
        provider = _provider(args)
        options = _query_options(args)
        if args.command == "query" and args.mode == "daily" and args.provider == "local":
            from .daily_report import benchmarks_for, daily_report

            report = daily_report(provider, asset, event_time, as_of=as_of, config=options["config"],
                                  benchmarks=(*benchmarks_for(asset), *([benchmark_asset.ticker] if benchmark_asset else [])))
            if args.format == "table":
                from .daily_table import format_daily_table

                print(format_daily_table(report), end="")
            else:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command == "query" and args.mode == "hourly" and args.provider == "local":
            from .daily_report import benchmarks_for, hourly_report
            report = hourly_report(provider, asset, event_time, as_of=as_of,
                                   benchmarks=(*benchmarks_for(asset), *([benchmark_asset.ticker] if benchmark_asset else [])))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.command == "query":
            result = query_event(provider, asset, event_time, as_of=as_of, **options)
            comparison = query_event(provider, benchmark_asset, event_time, as_of=as_of, **options) if benchmark_asset else None
            print(json.dumps(_result_dict(result, benchmark=comparison), ensure_ascii=False, indent=2))
            return 0
        articles = read_news(args.news)
        matched = [(item, match) for item in articles if (match := find_direct_match(item, asset))]
        output: list[dict] = []
        failures = 0
        for item, match in matched:
            try:
                if args.mode == "hourly" and args.provider == "local":
                    from .daily_report import benchmarks_for, hourly_report
                    row = hourly_report(provider, asset, item.published_at, as_of=as_of,
                        benchmarks=(*benchmarks_for(asset), *([benchmark_asset.ticker] if benchmark_asset else [])))
                    row.update({"article_id": item.article_id, "title": item.title, "source": item.source,
                                "url": item.url, "match": asdict(match)})
                    output.append(row)
                    continue
                result = query_event(provider, asset, item.published_at, as_of=as_of, **options)
                comparison = query_event(provider, benchmark_asset, item.published_at, as_of=as_of, **options) if benchmark_asset else None
                output.append(_result_dict(result, news=item, match=match, benchmark=comparison))
            except ValueError as exc:
                failures += 1
                output.append({
                    "article_id": item.article_id, "title": item.title,
                    "published_at": item.published_at.isoformat(),
                    "ticker": asset.ticker, "match": asdict(match),
                    "status": "error", "error": str(exc),
                })
        if args.format == "csv":
            if args.output:
                with _output_path(args.output).open("w", encoding="utf-8-sig", newline="") as handle:
                    _write_csv(output, handle)
            else:
                _write_csv(output, sys.stdout)
        else:
            text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output)
            if args.output:
                _output_path(args.output).write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
        print(f"articles={len(articles)} matched={len(matched)} queried={len(matched)-failures} failed={failures}", file=sys.stderr)
        return 0 if failures == 0 else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if provider is not None and hasattr(provider, "close"):
            provider.close()
