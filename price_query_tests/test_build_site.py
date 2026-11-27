from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from web.build_site import PRICE_QUERY_URL, ROOT, build


class SiteBuildTests(unittest.TestCase):
    def test_legacy_workflow_command_builds_only_the_redirect(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "site"
            subprocess.run(
                [sys.executable, str(ROOT / "web" / "build_site.py"),
                 "--news", str(root / "absent-news.csv"),
                 "--public-prices", str(root / "absent-prices.csv"),
                 "--output", str(output)],
                check=True, capture_output=True,
            )
            self.assertEqual({path.name for path in output.iterdir()}, {"index.html"})
            page = (output / "index.html").read_text(encoding="utf-8")
            self.assertIn(f'http-equiv="refresh" content="0; url={PRICE_QUERY_URL}"', page)
            self.assertIn(f'<a href="{PRICE_QUERY_URL}">', page)
            self.assertEqual(build(output).read_text(encoding="utf-8"), page)

    def test_existing_page_and_data_are_not_overwritten(self):
        with TemporaryDirectory() as temp:
            output = Path(temp)
            index = output / "index.html"
            index.write_text("<h1>Existing page</h1>", encoding="utf-8")
            prices = output / "price-history.csv"
            prices.write_text("existing prices", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty output directory"):
                build(output)
            self.assertEqual(index.read_text(encoding="utf-8"), "<h1>Existing page</h1>")
            self.assertEqual(prices.read_text(encoding="utf-8"), "existing prices")


if __name__ == "__main__":
    unittest.main()
