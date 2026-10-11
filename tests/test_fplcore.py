"""Only the exact FPLArmo author is monitored; extract complete API content."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import articles
import fplcore


def post(author="fplarmo", author_id=fplcore.ARMO_AUTHOR_ID):
    return {
        "id": 875, "slug": "gameweek-5-review", "status": "publish",
        "author": author_id, "date_gmt": "2026-10-01T04:24:16",
        "link": "https://fplcore.wordpress.com/2026/10/01/gameweek-5-review/",
        "title": {"rendered": "GW5 review &amp; analysis"},
        "excerpt": {"rendered": "<p>Key takeaways.</p>"},
        "content": {"rendered": '<p>First paragraph.</p><img src="https://example.com/chart.png"><p>Last paragraph.</p>', "protected": False},
        "_embedded": {
            "author": [{"id": author_id, "name": author, "slug": author}],
            "wp:featuredmedia": [{"source_url": "https://example.com/feature.png"}],
        },
    }


class FPLCoreTests(unittest.TestCase):
    def test_discovery_uses_author_filter_and_checks_embedded_identity(self):
        missing_author = post()
        missing_author.pop("_embedded")
        protected = post()
        protected["content"]["protected"] = True
        draft = post()
        draft["status"] = "draft"
        response = Mock()
        response.json.return_value = [post(), post("olbaud", 123), post("fplarmo fan"), missing_author, protected, draft]
        with patch.object(fplcore.requests, "get", return_value=response) as get:
            results = fplcore.discover_posts()
        self.assertEqual([p["id"] for p in results], [875])
        self.assertEqual(get.call_args.kwargs["params"]["author"], fplcore.ARMO_AUTHOR_ID)
        self.assertEqual(get.call_args.kwargs["params"]["status"], "publish")

    def test_api_content_preserves_final_paragraph_and_image_position(self):
        original = post()
        with patch.object(fplcore, "_posts", return_value=[copy.deepcopy(original)]) as fetch:
            result = fplcore.fetch_article("https://www.fplcore.com/blog/gameweek-5-review")
        fetch.assert_called_once_with(slug="gameweek-5-review", per_page=1)
        self.assertEqual(result["html"], original["content"]["rendered"])
        self.assertEqual(result["title"], "GW5 review & analysis")
        self.assertEqual(result["source_name"], "FPLArmo")
        self.assertEqual(result["feature_image"], "https://example.com/feature.png")
        self.assertEqual(result["url"], "https://www.fplcore.com/blog/gameweek-5-review")

    def test_protected_or_unpublished_content_is_never_extracted(self):
        for change in ("protected", "draft"):
            item = post()
            if change == "protected":
                item["content"]["protected"] = True
            else:
                item["status"] = "draft"
            with self.subTest(change=change), patch.object(fplcore, "_posts", return_value=[item]):
                self.assertIsNone(fplcore.fetch_article(fplcore.post_url(item)))

    def test_article_extractor_uses_api_body_without_reader_mode(self):
        with (
            patch.object(fplcore, "_posts", return_value=[post()]),
            patch.object(articles, "_drop_recurring_images", side_effect=lambda article: article),
            patch.object(articles.trafilatura, "extract") as reader,
        ):
            result = articles.fetch_article("https://www.fplcore.com/blog/gameweek-5-review")
        reader.assert_not_called()
        self.assertIn("First paragraph.", result["html"])
        self.assertIn("Last paragraph.", result["html"])
        self.assertLess(result["html"].index("chart.png"), result["html"].index("Last paragraph."))
        self.assertEqual(result["images"], ["https://example.com/chart.png"])

    def test_listing_and_other_hosts_are_not_article_urls(self):
        for url in (fplcore.BLOG_URL, fplcore.BLOG_URL + "?page=2", "https://other.com/blog/review"):
            self.assertFalse(fplcore.is_article_url(url))


if __name__ == "__main__":
    unittest.main()
