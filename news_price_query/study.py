"""Descriptive coverage and raw-return summary for event-query output."""

from statistics import mean, median


def summarize(records: list[dict]) -> dict:
    """Summarize available observations without treating pending values as zero."""
    success = [row for row in records if row.get("status") != "error" and "windows" in row]
    labels = {label for row in success for label in row["windows"]}
    windows = {}
    for label in sorted(labels):
        observations = [row["windows"][label] for row in success if label in row["windows"]]
        values = [item["return_pct"] for item in observations if item.get("return_pct") is not None]
        on_time_values = [
            item["return_pct"] for item in observations
            if item.get("status") in {"on_time", "on_date"} and item.get("return_pct") is not None
        ]
        excess_values = [
            item["excess_return_pct"] for item in observations
            if item.get("excess_return_pct") is not None
        ]
        windows[label] = {
            "available": len(values),
            "on_time_or_date": sum(item.get("status") in {"on_time", "on_date"} for item in observations),
            "deferred": sum(item.get("status") in {"deferred", "deferred_back"} for item in observations),
            "missing": sum(item.get("status") == "missing" for item in observations),
            "pending": sum(item.get("status") == "pending" for item in observations),
            "mean_return_pct": mean(values) if values else None,
            "median_return_pct": median(values) if values else None,
            "on_time_mean_return_pct": mean(on_time_values) if on_time_values else None,
            "available_excess": len(excess_values),
            "mean_excess_return_pct": mean(excess_values) if excess_values else None,
        }
    return {
        "events_total": len(records),
        "events_success": len(success),
        "events_error": len(records) - len(success),
        "windows": windows,
        "interpretation": "Descriptive raw price returns; no causal or predictive claim.",
    }
