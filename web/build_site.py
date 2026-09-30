"""Build a GitHub Pages artifact from the repository's existing news CSV."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from news_price_query.assets import ASSETS  # noqa: E402
from news_price_query.news import find_direct_match, read_news  # noqa: E402
from news_price_query.prices import CsvPriceProvider  # noqa: E402


def build(news_path: Path, output: Path, public_prices: Path | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for filename in ("index.html", "styles.css", "core.mjs", "app.mjs"):
        shutil.copy2(ROOT / "web" / filename, output / filename)
    (output / ".nojekyll").write_text("", encoding="utf-8")

    articles = read_news(news_path)
    matches = []
    for article in articles:
        for asset in ASSETS:
            if asset.role != "equity":
                continue
            match = find_direct_match(article, asset)
            if match:
                matches.append({
                    "article_id": article.article_id,
                    "ticker": asset.ticker,
                    "title": article.title,
                    "source": article.source,
                    "url": article.url,
                    "published_at": article.published_at.isoformat(),
                    "match_type": match.kind,
                    "evidence": match.evidence,
                    "verified_by_human": False,
                })
    index = {"articles_total": len(articles), "matches": matches}
    (output / "news-index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    price_file = output / "price-history.csv"
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "news_articles_total": len(articles),
        "news_matches_total": len(matches),
        "news_latest_published_at": max((item.published_at.isoformat() for item in articles), default=None),
        "public_prices_available": False,
        "latest_price_at": None,
        "price_bars_total": 0,
    }
    if public_prices and public_prices.is_file():
        provider = CsvPriceProvider(public_prices)
        if not provider._bars:
            raise ValueError("Public price CSV has no bars")
        shutil.copy2(public_prices, price_file)
        manifest["public_prices_available"] = True
        manifest["latest_price_at"] = max(item.timestamp for item in provider._bars).isoformat()
        manifest["price_bars_total"] = len(provider._bars)
    elif price_file.exists():
        price_file.unlink()
    (output / "site-data.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news", type=Path, default=ROOT / "data" / "news.csv")
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    parser.add_argument("--public-prices", type=Path, default=ROOT / "data" / "public_prices.csv")
    args = parser.parse_args()
    print(json.dumps(build(args.news, args.output, args.public_prices), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
