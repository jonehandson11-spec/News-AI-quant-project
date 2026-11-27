"""Build the GitHub Pages redirect to the hosted price query service."""

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_QUERY_URL = "https://news-price-query-en.onrender.com/"
REDIRECT_PAGE = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="0; url={PRICE_QUERY_URL}">
  <link rel="canonical" href="{PRICE_QUERY_URL}">
  <title>Hong Kong &amp; Oil Markets</title>
</head>
<body>
  <main>
    <h1>Hong Kong &amp; Oil Markets</h1>
    <p><a href="{PRICE_QUERY_URL}">Open the price lookup</a></p>
  </main>
</body>
</html>
"""


def build(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    page = output / "index.html"
    entries = tuple(output.iterdir())
    if entries:
        if (entries == (page,) and not page.is_symlink() and page.is_file()
                and page.read_text(encoding="utf-8", errors="replace") == REDIRECT_PAGE):
            return page
        raise ValueError("Use a new or empty output directory to avoid publishing stale files.")
    with page.open("x", encoding="utf-8") as handle:
        handle.write(REDIRECT_PAGE)
    return page


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # Accept the previous workflow command without reading legacy data inputs.
    parser.add_argument("--news", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    parser.add_argument("--public-prices", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        print(build(args.output))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
