"""Human-readable start/end table for a daily_report payload (CLI --format table)."""
from __future__ import annotations

MISSING = "缺失"


def _price(value) -> str:
    if value is None:
        return MISSING
    text = f"{value:,.4f}"
    while text.endswith("0") and len(text.split(".")[1]) > 2:
        text = text[:-1]
    return text


def _pct(value) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _day(value) -> str:
    return value[:10] if value else MISSING


def _leg(row: dict, prefix: str = "") -> str:
    start, end = row.get(f"{prefix}start_price"), row.get(f"{prefix}end_price")
    if row.get("status") in ("pending", "expired") and end is None:
        end_text = "未到期" if row.get("status") == "pending" else "合约已到期"
        return f"{_day(row.get(f'{prefix}start_at'))} {_price(start)} → {end_text}"
    return (f"{_day(row.get(f'{prefix}start_at'))} {_price(start)} → "
            f"{_day(row.get(f'{prefix}end_at'))} {_price(end)}")


def format_daily_table(report: dict) -> str:
    lines = [f"{report.get('ticker', '')}  发布 {report.get('published_at_local', '')}  "
             f"{report.get('currency', '')}" + (f" {report['unit']}" if report.get("unit") else "")]
    contract = report.get("contract") or {}
    if contract.get("ticker"):
        lines.append(f"固定合约 {contract['ticker']}" + (f"（{contract['note']}）" if contract.get("note") else ""))
    for label, row in (report.get("windows") or {}).items():
        lines.append(f"{label:<11} {_leg(row)}  原始 {_pct(row.get('return_pct'))}"
                     f"  复权 {_leg(row, 'adj_')} {_pct(row.get('adj_return_pct'))}  [{row.get('status', '')}]")
        cont = row.get("continuous")
        if cont:
            roll = " 跨换月" if cont.get("crosses_roll") else ""
            lines.append(f"{'':<11}   连续合约 {_leg(cont)} {_pct(cont.get('return_pct'))}{roll}")
        for code, bench in (report.get("benchmarks") or {}).items():
            other = (bench.get("windows") or {}).get(label)
            if other:
                lines.append(f"{'':<11}   {code} {_leg(other)} {_pct(other.get('return_pct'))}"
                             f"  超额 {_pct(other.get('excess_return_pct'))}"
                             f"  复权超额 {_pct(other.get('adj_excess_return_pct'))}")
    return "\n".join(lines) + "\n"
