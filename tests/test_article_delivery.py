"""Exercise both real article-delivery branches without initializing Telethon."""
import ast
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))
from translator import ContentIncompleteError


async def inline_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


class ArticleDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Importing bot.py creates a session and initializes production paths.
        # Load just the production importer function to exercise its control
        # flow with mocked services, without those module-level side effects.
        path = Path(__file__).resolve().parents[1] / "teleadmin_project" / "bot.py"
        tree = ast.parse(path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_publish_article_from_url")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.photo = Path(directory.name) / "photo.jpg"
        self.photo.touch()
        self.article = {
            "title": "Review", "url": "https://www.fplcore.com/blog/review",
            "html": "<p>Source text</p>", "feature_image": "https://example.com/photo.jpg",
            "images": [], "source_name": "FPLArmo",
        }
        self.services = Mock()
        self.services.fetch_article.return_value = self.article
        self.services.prepare_article_html.return_value = ("<p>Source text</p>", [])
        self.services.article_link_targets.return_value = set()
        self.services.sanitize_article_links.side_effect = lambda text, links: text
        self.services.strip_image_markers.side_effect = lambda text: text
        self.services.publish_to_telegraph.return_value = "https://telegra.ph/review"
        self.queue = AsyncMock()
        self.deliver = AsyncMock()
        self.translate = AsyncMock(return_value={"title": "عنوان", "summary": "خلاصه", "body": "متن فارسی"})
        self.namespace = {
            "asyncio": SimpleNamespace(to_thread=inline_thread), "articles": self.services,
            "translator": SimpleNamespace(translate_article=self.translate),
            "Path": Path, "logger": Mock(), "ContentIncompleteError": ContentIncompleteError,
            "_article_title": lambda title, fallback: title or fallback,
            "_fix_unclosed_tags": lambda text: text, "_strip_quotes": lambda text: text,
            "_format_short_article_post": Mock(return_value="Short post"),
            "_format_telegraph_post": Mock(return_value="Telegraph post"),
            "_rendered_text_length": len, "_caption_length": len, "_MEDIA_CAPTION_LIMIT": 1024,
            "_download_remote_media": Mock(return_value=str(self.photo)),
            "_send_to_target": self.queue, "_send_notification": AsyncMock(),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), self.namespace)
        self.publish = self.namespace["_publish_article_from_url"]

    def choose_format(self, long_article):
        self.namespace["_format_short_article_post"].return_value = "x" * 1025 if long_article else "Short post"

    async def test_short_and_telegraph_articles_use_custom_delivery(self):
        for long_article in (False, True):
            with self.subTest(telegraph=long_article):
                self.photo.touch()
                self.deliver.reset_mock()
                self.choose_format(long_article)
                self.assertTrue(await self.publish(self.article["url"], deliver_post=self.deliver))
                self.deliver.assert_awaited_once_with("Telegraph post" if long_article else "Short post", file_path=str(self.photo))
                self.queue.assert_not_awaited()
                self.assertFalse(self.photo.exists())
        self.services.publish_to_telegraph.assert_called_once()

    async def test_other_articles_keep_the_existing_review_queue(self):
        for long_article in (False, True):
            with self.subTest(telegraph=long_article):
                self.photo.touch()
                self.queue.reset_mock()
                self.choose_format(long_article)
                self.assertTrue(await self.publish(self.article["url"]))
                self.queue.assert_awaited_once_with("Telegraph post" if long_article else "Short post", file_path=str(self.photo), event=None, queue=True)

    async def test_delivery_failure_cleans_up_photo_and_propagates_for_retry(self):
        self.deliver.side_effect = RuntimeError("Forward failed")
        with self.assertRaisesRegex(RuntimeError, "Forward failed"):
            await self.publish(self.article["url"], deliver_post=self.deliver)
        self.assertFalse(self.photo.exists())
        self.queue.assert_not_awaited()
        self.namespace["_send_notification"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
