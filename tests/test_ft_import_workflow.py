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
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(io.StringIO()):
                try:
                    exec(compile(code, "FT import summary", "exec"), {})
                except SystemExit as error:
                    exit_code = error.code
            return summary.read_text(encoding="utf-8"), exit_code

    def test_partial_local_failure_is_reported_after_publication_without_private_text(self):
        private = "FAKE_SECRET_AND_LOCAL_PATH_C:\\private\\cookie.txt"
        summary, failure = self.run_summary({
            "status": "partial", "after": 513, "inserted": 4, "duplicates": 2,
            "deferred_cap": 0, "batch_id": "b" * 64,
            "errors": [private], "article_text": private, "batch_file": private,
        }, published=True)
        self.assertIsNotNone(failure)
        self.assertIn("New articles: 4; duplicates: 2", summary)
        self.assertIn("resulting total: 513", summary)
        self.assertIn("publication completed: **True**", summary)
        self.assertIn("were saved before this failure", summary)
        self.assertIn("b" * 64, summary)
        self.assertNotIn(private, summary)

    def test_failed_publication_is_not_reported_as_saved(self):
        summary, failure = self.run_summary({
            "status": "failed", "after": 509, "inserted": 0,
        }, published=False)
        self.assertIsNotNone(failure)
        self.assertIn("publication did not complete", summary)
        self.assertNotIn("were saved", summary)

    def test_missing_result_does_not_claim_empty_database_or_success(self):
        summary, failure = self.run_summary(None, published=False)
        self.assertIsNone(failure)
        self.assertIn("**not_completed**", summary)
        self.assertIn("publication completed: **False**", summary)
        self.assertNotIn("resulting total:", summary)


if __name__ == "__main__":
    unittest.main()
