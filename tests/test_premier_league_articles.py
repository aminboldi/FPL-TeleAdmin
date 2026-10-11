"""PL app landing pages and lazy gallery photos use the shared importer."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))
import articles


URL = "https://www.premierleague.com/en/news/4730172"
SHARE = "https://premierleague.app.link/e/mXNcn2yv46b"
PHOTO = "https://resources.premierleague.pulselive.com/photo-resources/2026/10/08/selection.png"
PAGE = '''
<h1 class="article__header-title">Scout Selection</h1>
<div class="article__header-image"><img src="/header.jpg"></div>
<div class="article__content">
  <p>Saka is selected as captain.</p>
  <div class="articleWidget full-width"><div class="photo-gallery"
    data-media-type="photo" data-media-id="4730232"></div></div>
  <h5>David Raya (Arsenal) £6.1m</h5><p>First selection.</p>
  <div><figure><picture><img src="/nested.png"></picture></figure></div>
  <div class="articleWidget"><div class="embeddable-article">Related story</div></div>
  <div class="image-promo"><img src="/banner.jpg">Promotion</div>
  <p>Final selection.</p>
</div>'''


def response(url, text="", data=None):
    result = Mock(url=url, text=text, status_code=200, ok=True, headers={"content-type": "text/html"})
    result.json.return_value = data
    return result


class PremierLeagueArticleTests(unittest.TestCase):
    def setUp(self):
        # Catalog lookups and image bookkeeping are unrelated to extraction.
        for replacement in (
            patch.object(articles, "reroute_internal_links", return_value=set()),
            patch.object(articles, "_drop_recurring_images", side_effect=lambda article: article),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def fetch(self, url=URL):
        def get(request_url, **kwargs):
            if request_url == SHARE:
                return response(SHARE, f'<meta property="og:url" content="{URL}?utm_source=x&amp;other=1">')
            if request_url == URL or request_url == "https://t.co/example":
                return response(URL, PAGE)
            self.assertEqual(request_url, "https://api.premierleague.com/content/premierleague/photo/EN/4730232")
            return response(request_url, data={"onDemandUrl": PHOTO, "imageUrl": "https://example.com/fallback.png"})

        with patch.object(articles.requests, "get", side_effect=get):
            return articles.fetch_article(url)

    def test_bare_share_link_is_detected_and_resolved_from_metadata(self):
        text = "The Scout has made their selection 👉 premierleague.app.link/e/mXNcn2yv46b"
        self.assertTrue(articles.is_pl_article_url(text))
        self.assertEqual(articles.resolve_url(text), SHARE)
        article = self.fetch(SHARE.removeprefix("https://"))
        self.assertEqual(article["url"], URL)
        self.assertIn(PHOTO, article["images"])

    def test_hidden_share_link_is_detected(self):
        self.assertTrue(articles.is_pl_article_url("Read more", [SimpleNamespace(url=SHARE)]))

    def test_gallery_and_nested_images_keep_their_body_positions(self):
        article = self.fetch()
        body = article["html"]
        self.assertLess(body.index("Saka"), body.index(PHOTO))
        self.assertLess(body.index(PHOTO), body.index("David Raya"))
        self.assertIn("<h4>David Raya", body)
        self.assertIn("Final selection.", body)
        self.assertNotIn("Related story", body)
        self.assertNotIn("Promotion", body)
        self.assertEqual(article["images"], [PHOTO, "https://www.premierleague.com/nested.png"])
        self.assertEqual(article["feature_image"], "https://www.premierleague.com/header.jpg")
        marked, images = articles.prepare_article_html(articles.build_general_article_html(article))
        self.assertEqual(images, article["images"])
        self.assertLess(marked.index("Saka"), marked.index("[[TELEADMIN_IMAGE_1]]"))
        self.assertLess(marked.index("[[TELEADMIN_IMAGE_1]]"), marked.index("David Raya"))

    def test_general_redirect_to_pl_uses_the_same_gallery_parser(self):
        article = self.fetch("https://t.co/example")
        self.assertIn(PHOTO, article["images"])
        self.assertEqual(article["url"], URL)

    def test_already_rendered_photo_does_not_need_the_photo_api(self):
        rendered = PAGE.replace('data-media-id="4730232"></div>', f'data-media-id="4730232"><img src="{PHOTO}"></div>')
        with patch.object(articles.requests, "get") as get:
            article = articles._parse_pl_article(rendered, URL)
        get.assert_not_called()
        self.assertIn(PHOTO, article["images"])

    def test_lazy_inline_image_is_kept_and_empty_image_is_removed(self):
        rendered = PAGE.replace('<img src="/nested.png">', '<img data-src="/nested.png"><img src="">')
        with patch.object(articles.requests, "get", return_value=response("photo", data={"onDemandUrl": PHOTO})):
            article = articles._parse_pl_article(rendered, URL)
        self.assertEqual(article["images"], [PHOTO, "https://www.premierleague.com/nested.png"])

    def test_share_metadata_must_point_to_a_pl_news_article(self):
        for destination in ("https://example.com/en/news/4730172", "https://www.premierleague.com/en/fixtures"):
            with self.subTest(destination=destination):
                landing = response(SHARE, f'<meta property="og:url" content="{destination}">')
                with patch.object(articles.requests, "get", return_value=landing):
                    self.assertIsNone(articles.resolve_pl_share_url(SHARE))

    def test_failed_photo_fetch_is_retryable_instead_of_silently_losing_the_image(self):
        with patch.object(articles.requests, "get", side_effect=requests.ConnectionError("Photo API unavailable")):
            with self.assertRaises(requests.ConnectionError):
                articles._parse_pl_article(PAGE, URL)

    def test_failed_pl_article_fetch_is_retryable(self):
        with patch.object(articles.requests, "get", side_effect=requests.ConnectionError("PL unavailable")):
            with self.assertRaises(requests.ConnectionError):
                articles.fetch_article(URL)


if __name__ == "__main__":
    unittest.main()
