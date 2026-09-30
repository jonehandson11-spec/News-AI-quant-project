import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from news_price_query.cli import main
from news_price_query.study import summarize


class StudyTests(unittest.TestCase):
    def test_summary_counts_missing_pending_and_deferred_without_fabricating_returns(self):
        records = [
            {"article_id": "a", "ticker": "1211.HK", "windows": {
                "after_1h": {"return_pct": 2.0, "excess_return_pct": 1.0, "status": "on_time"},
                "after_12h": {"return_pct": 3.0, "status": "deferred"},
            }},
            {"article_id": "b", "ticker": "1211.HK", "windows": {
                "after_1h": {"return_pct": -1.0, "excess_return_pct": 1.0, "status": "on_time"},
                "after_12h": {"return_pct": None, "status": "missing"},
            }},
            {"article_id": "c", "ticker": "1211.HK", "windows": {
                "after_1h": {"return_pct": None, "status": "pending"},
                "after_12h": {"return_pct": None, "status": "pending"},
            }},
            {"article_id": "d", "status": "error", "error": "no baseline"},
        ]
        result = summarize(records)
        self.assertEqual(result["events_total"], 4)
        self.assertEqual(result["events_success"], 3)
        self.assertEqual(result["events_error"], 1)
        one_hour = result["windows"]["after_1h"]
        self.assertEqual(one_hour["available"], 2)
        self.assertEqual(one_hour["pending"], 1)
        self.assertEqual(one_hour["mean_return_pct"], 0.5)
        self.assertEqual(one_hour["median_return_pct"], 0.5)
        self.assertEqual(one_hour["on_time_mean_return_pct"], 0.5)
        self.assertEqual(one_hour["available_excess"], 2)
        self.assertEqual(one_hour["mean_excess_return_pct"], 1.0)
        twelve_hour = result["windows"]["after_12h"]
        self.assertEqual(twelve_hour["deferred"], 1)
        self.assertEqual(twelve_hour["missing"], 1)
        self.assertEqual(twelve_hour["available"], 1)
        self.assertIsNone(twelve_hour["on_time_mean_return_pct"])

    def test_study_cli_reads_jsonl_and_emits_sample_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(json.dumps({"article_id": "a", "ticker": "1211.HK", "windows": {
                "after_1h": {"return_pct": 1.5, "status": "on_time"}
            }}) + "\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["study", "--input", str(path)])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["events_success"], 1)
        self.assertEqual(result["windows"]["after_1h"]["available"], 1)


if __name__ == "__main__":
    unittest.main()
