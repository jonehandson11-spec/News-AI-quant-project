"""Offline checks for serialized FT import publication and safe job summaries."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def steps(text):
    """Read the named, literal run blocks in these checked-in workflows."""
    result = {}
    current = None
    for line in text.splitlines():
        if line.startswith("      - name: "):
            current = line.removeprefix("      - name: ")
            result[current] = []
        elif current:
            result[current].append(line)
    return {name: "\n".join(lines) for name, lines in result.items()}


class FTImportWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/import-ft.yml").read_text(encoding="utf-8")
        cls.daily = (ROOT / ".github/workflows/crawl.yml").read_text(encoding="utf-8")
        cls.steps = steps(cls.workflow)

    def test_inbox_import_and_daily_writes_share_one_serial_queue(self):
        self.assertIn("branches: [codex/ft-inbox]", self.workflow)
        # A retry may create a new commit pointing at the same batch blob.
        # Branch-only filtering must still enqueue that import.
        self.assertNotIn("paths:", self.workflow)
        self.assertNotIn("paths-ignore:", self.workflow)
        for workflow in (self.workflow, self.daily):
            self.assertIn("concurrency:\n  group: news-data\n  cancel-in-progress: false", workflow)
        self.assertIn("ref: main", self.steps["Check out the latest shared database"])
        self.assertNotIn("FT_COOKIE", self.workflow)
        self.assertNotIn("secrets.", self.workflow)
        self.assertNotIn("continue-on-error", self.workflow)
        names = list(self.steps)
        self.assertLess(names.index("Validate the existing shared database"),
                        names.index("Merge validated local articles into the latest database"))
        self.assertLess(names.index("Validate the merged database, CSV, and metadata"),
                        names.index("Save the merged shared data"))
        self.assertLess(names.index("Save the merged shared data"),
                        names.index("Report the merge and local FT health"))

    def test_batch_is_read_from_exact_event_commit_as_a_file_and_never_executed(self):
        read = self.steps["Read the submitted batch from its exact commit"]
        self.assertIn("BATCH_COMMIT: ${{ github.sha }}", read)
        self.assertIn('"$BATCH_COMMIT" =~ ^[0-9a-f]{40}$', read)
        self.assertIn('git fetch --no-tags --depth=1 origin "$BATCH_COMMIT"', read)
        self.assertIn('git show FETCH_HEAD:incoming/ft-batch.json > "$RUNNER_TEMP/ft-batch.json"', read)
        merge = self.steps["Merge validated local articles into the latest database"]
        self.assertIn("python3 scripts/ft_local.py merge --root .", merge)
        self.assertIn('--batch-file "$RUNNER_TEMP/ft-batch.json"', merge)
        self.assertIn('--result-file "$RUNNER_TEMP/ft-merge-result.json"', merge)
        publish = self.steps["Save the merged shared data"]
        self.assertIn("git add -- data/", publish)
        self.assertIn("git push origin HEAD:main", publish)
        self.assertNotIn("--force", publish)
        recovery = self.steps["Preserve shared data for recovery"]
        self.assertNotIn("ft-batch.json", recovery)
        self.assertNotIn("incoming/", recovery)

    def run_summary(self, result, *, published):
        block = self.steps["Report the merge and local FT health"]
        match = re.search(r"          python3 - <<'PY'\n(.*?)\n          PY", block, re.S)
        self.assertIsNotNone(match)
        code = "\n".join(line[10:] for line in match.group(1).splitlines())
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            summary = temporary / "summary.md"
            if result is not None:
                (temporary / "ft-merge-result.json").write_text(json.dumps(result), encoding="utf-8")
            environment = {
                "RUNNER_TEMP": str(temporary), "GITHUB_STEP_SUMMARY": str(summary),
                "BATCH_COMMIT": "a" * 40, "PUBLISH_COMPLETED": str(published).lower(),
            }
            exit_code = None
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
                try:
                    exec(compile(code, "FT import summary", "exec"), {})
                except SystemExit as error:
                    exit_code = error.code
            return summary.read_text(encoding="utf-8"), exit_code, output.getvalue()

    def test_partial_collection_warns_after_successful_import_without_private_text(self):
        private = "FAKE_SECRET_AND_LOCAL_PATH_C:\\private\\cookie.txt"
        summary, failure, output = self.run_summary({
            "status": "partial", "after": 513, "inserted": 4, "duplicates": 2,
            "deferred_cap": 0, "batch_id": "b" * 64,
            "collection_status": "partial", "collection_reason": "invalid_or_incomplete_article",
            "collection_stopped": False,
            "errors": [private], "article_text": private, "batch_file": private,
        }, published=True)
        self.assertIsNone(failure)
        self.assertIn("New articles: 4; duplicates: 2", summary)
        self.assertIn("resulting total: 513", summary)
        self.assertIn("Import: **success**", summary)
        self.assertIn("publication completed: **True**", summary)
        self.assertIn("Submitted batch collection: **partial**", summary)
        self.assertIn("reason: `invalid_or_incomplete_article`; stopped: **False**", summary)
        self.assertIn("::warning title=Local FT collection::", output)
        self.assertIn("b" * 64, summary)
        self.assertNotIn(private, summary + output)

    def test_failed_publication_is_not_reported_as_saved(self):
        summary, failure, output = self.run_summary({
            "status": "failed", "after": 509, "inserted": 0,
            "collection_status": "failed", "collection_reason": "auth_expired",
            "collection_stopped": True,
        }, published=False)
        # The original merge/validation/push step keeps the job failed.
        self.assertIsNone(failure)
        self.assertIn("Import: **not_completed**", summary)
        self.assertIn("publication did not complete", summary)
        self.assertNotIn("Import: **success**", summary)
        self.assertNotIn("Import publication completed", summary)
        self.assertIn("::warning", output)

    def test_missing_result_does_not_claim_empty_database_or_success(self):
        summary, failure, output = self.run_summary(None, published=False)
        self.assertIsNone(failure)
        self.assertIn("**not_completed**", summary)
        self.assertIn("publication completed: **False**", summary)
        self.assertNotIn("resulting total:", summary)
        self.assertNotIn("Import: **success**", summary)
        self.assertEqual(output, "")

    def test_real_collection_stops_remain_visible_after_import(self):
        for reason in ("auth_expired", "access_denied", "rate_limited", "login_or_subscription_required"):
            for status in ("partial", "failed"):
                with self.subTest(reason=reason, status=status):
                    summary, failure, output = self.run_summary({
                        "status": status, "collection_status": status,
                        "collection_reason": reason, "collection_stopped": True,
                    }, published=True)
                    self.assertIsNone(failure)
                    self.assertIn("Import: **success**", summary)
                    self.assertIn(f"Submitted batch collection: **{status}**", summary)
                    self.assertIn(f"reason: `{reason}`; stopped: **True**", summary)
                    self.assertIn(f"reason: {reason}; stopped: True", output)

    def test_summary_does_not_echo_untrusted_collection_fields(self):
        private = "FAKE_PRIVATE_TEXT\n::error::untrusted"
        for fields in (
            {"collection_status": "partial", "collection_reason": private},
            {"collection_status": private, "collection_reason": [private]},
        ):
            with self.subTest(fields=fields):
                summary, failure, output = self.run_summary({
                    "status": "partial", "collection_stopped": private, **fields,
                }, published=True)
                self.assertIsNone(failure)
                self.assertNotIn(private, summary + output)
                self.assertIn("reason: `not_reported`; stopped: **False**", summary)

    def test_completed_collection_has_no_warning(self):
        summary, failure, output = self.run_summary({
            "status": "success", "collection_status": "complete",
            "collection_reason": None, "collection_stopped": False,
        }, published=True)
        self.assertIsNone(failure)
        self.assertIn("Import: **success**", summary)
        self.assertIn("Submitted batch collection: **complete**", summary)
        self.assertEqual(output, "")

    def test_stale_retry_warns_about_batch_without_claiming_to_replace_health(self):
        summary, failure, output = self.run_summary({
            "status": "stale_batch", "after": 513, "inserted": 0, "duplicates": 4,
            "collection_status": "partial", "collection_reason": "auth_expired",
            "collection_stopped": True, "health_preserved": True,
        }, published=True)
        self.assertIsNone(failure)
        self.assertIn("Import: **success**", summary)
        self.assertIn("New articles: 0; duplicates: 4", summary)
        self.assertIn("An equally recent or newer source health record was retained", summary)
        self.assertIn("::warning", output)


if __name__ == "__main__":
    unittest.main()
