"""Text-only source posts should not become articles just by being buffered."""
import ast
import asyncio
import html
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bs4 import BeautifulSoup
from telethon.tl.types import MessageEntityBlockquote, MessageEntityTextUrl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))
from translator import TranslationError


class TextPostRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Load the actual handlers and text helpers without starting a user
        # session, touching production databases, or calling a translation API.
        path = Path(__file__).resolve().parents[1] / "teleadmin_project" / "bot.py"
        names = {
            "handle_new_message", "_finish_chunks", "_forward_message",
            "_message_to_html", "_escape_html", "_strip_html_tags", "_strip_hashtags",
            "_strip_quotes", "_build_caption",
            "_strip_visible_urls", "_extract_urls", "_extract_urls_from_message",
            "_format_telegraph_post", "_format_short_article_post",
        }
        tree = ast.parse(path.read_text())
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        constants = {"_HASHTAG_RE", "_URL_RE", "_ARTICLE_SOURCE_THRESHOLD", "_CHUNK_TIMEOUT", "AI_SIGNATURE"}
        functions.extend(
            node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)
        )
        self.translation = AsyncMock(return_value="خبر کوتاه\nخط دوم")
        self.article_translation = AsyncMock(return_value={"title": "عنوان", "summary": "خلاصه", "body": "متن بلند"})
        self.publish = Mock(return_value="https://telegra.ph/long-post")
        self.send = AsyncMock()
        self.notify = AsyncMock()
        self.automatic = AsyncMock(return_value=False)
        self.url_articles = AsyncMock()
        self.prepare_article = Mock(side_effect=lambda html: (html, set()))
        self.namespace = {
            "asyncio": SimpleNamespace(sleep=AsyncMock(), create_task=asyncio.create_task),
            "re": re, "html_lib": html, "logger": Mock(), "TranslationError": TranslationError,
            "MessageEntityBlockquote": MessageEntityBlockquote,
            "MessageEntityTextUrl": MessageEntityTextUrl,
            "_chunk_buffer": {}, "_chunk_tasks": {},
            "_refresh_translator_model": Mock(), "_try_handle_automatic_content": self.automatic,
            "_display_link_url": AsyncMock(side_effect=lambda urls: urls[0] if urls else None),
            "_prepare_article_source": self.prepare_article,
            "_fix_unclosed_tags": lambda text: text,
            "_article_title": lambda title: title,
            "_catalog_source_tag": Mock(return_value="Source channel"),
            "_format_telegraph_post": Mock(return_value="Article caption"),
            "_strip_article_images": lambda text: text,
            "_telegraph_to_telegram_html": lambda text: text,
            "_has_uploadable_media": Mock(return_value=False),
            "_send_to_target": self.send, "_send_notification": self.notify,
            "_target_channel": Mock(return_value="@EPL_Fantasy"),
            "_maybe_post_article": self.url_articles,
            "translator": SimpleNamespace(translate=self.translation, translate_article=self.article_translation),
            "articles": SimpleNamespace(publish_to_telegraph=self.publish, sanitize_article_links=lambda html, links: html),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), self.namespace)
        # Route tests use a fixed article caption; formatter tests call the
        # separately retained production formatter below.
        self.format_telegraph = self.namespace["_format_telegraph_post"]
        self.namespace["_format_telegraph_post"] = Mock(return_value="Article caption")

    def event(self, text, entities=None):
        return SimpleNamespace(
            chat_id=123,
            message=SimpleNamespace(text=text, media=None, grouped_id=None, entities=entities or []),
        )

    async def process(self, events):
        for event in events:
            await self.namespace["handle_new_message"](event)
        await self.namespace["_chunk_tasks"][123]
        self.assertEqual(self.namespace["_chunk_buffer"], {})
        self.assertEqual(self.namespace["_chunk_tasks"], {})

    async def test_two_line_source_post_uses_plain_translation_and_text_queue(self):
        event = self.event("Saka is back in training.\nHe should be available this weekend.")
        await self.process([event])
        self.translation.assert_awaited_once_with(event.message.text)
        self.article_translation.assert_not_awaited()
        self.publish.assert_not_called()
        self.prepare_article.assert_not_called()
        self.send.assert_awaited_once_with("خبر کوتاه\nخط دوم\n\n@EPL_Fantasy | ✨AI", event=event, queue=True)
        self.notify.assert_awaited_once()

    async def test_short_merged_text_still_uses_plain_translation(self):
        events = [self.event("First short update."), self.event("Second short update.")]
        await self.process(events)
        self.translation.assert_awaited_once_with("First short update.\nSecond short update.")
        self.article_translation.assert_not_awaited()
        self.publish.assert_not_called()

    async def test_exact_threshold_stays_inline(self):
        await self.process([self.event("A" * 940)])
        self.translation.assert_awaited_once()
        self.article_translation.assert_not_awaited()

    async def test_true_long_chunks_still_produce_one_article(self):
        events = [self.event("A" * 600), self.event("B" * 600)]
        await self.process(events)
        self.translation.assert_not_awaited()
        self.article_translation.assert_awaited_once_with("A" * 600 + "\n" + "B" * 600)
        self.publish.assert_called_once()
        self.send.assert_awaited_once_with("Article caption", event=events[0], queue=True)

    async def test_html_formatting_does_not_push_short_text_above_threshold(self):
        event = self.event("A" * 930, [MessageEntityBlockquote(0, 930)])
        await self.process([event])
        self.translation.assert_awaited_once_with("<blockquote>" + "A" * 930 + "</blockquote>")
        self.article_translation.assert_not_awaited()

    async def test_removed_hashtags_and_url_do_not_force_article_translation(self):
        url = "https://example.com/" + "a" * 950
        event = self.event(f"Short update.\n{url}\n#FPL #News")
        await self.process([event])
        self.translation.assert_awaited_once_with("Short update.")
        self.article_translation.assert_not_awaited()
        self.publish.assert_not_called()
        self.url_articles.assert_awaited_once_with(event.message.text, event, urls=[url])

    async def test_query_string_url_is_removed_before_translation(self):
        url = "https://example.com/article?gameweek=6&player=123"
        await self.process([self.event(f"Short update.\n{url}")])
        self.translation.assert_awaited_once_with("Short update.")
        caption = self.send.await_args.args[0]
        parsed = BeautifulSoup(caption, "html.parser")
        self.assertNotIn(url, parsed.get_text())
        self.assertEqual([a["href"] for a in parsed.find_all("a")], [url])

    async def test_bare_premier_league_share_link_is_sent_to_article_detection(self):
        text = (
            "An XI to build your Second Chance League team around 👀\n\n"
            "The Scout has made their selection ahead of Gameweek 6 👉 "
            "premierleague.app.link/e/mXNcn2yv46b"
        )
        event = self.event(text)
        await self.process([event])
        url = "https://premierleague.app.link/e/mXNcn2yv46b"
        self.url_articles.assert_awaited_once_with(text, event, urls=[url])
        self.assertNotIn("premierleague.app.link", self.translation.await_args.args[0])
        self.assertEqual(BeautifulSoup(self.send.await_args.args[0], "html.parser").find("a")["href"], url)

    def test_hidden_bare_link_gets_https_and_uppercase_scheme_is_not_duplicated(self):
        event = self.event("HTTPS://example.com/read", [MessageEntityTextUrl(0, 4, "premierleague.app.link/e/hidden")])
        self.assertEqual(self.namespace["_extract_urls"](event), [
            "HTTPS://example.com/read", "https://premierleague.app.link/e/hidden",
        ])

    async def test_links_in_later_chunks_are_included_only_in_footer(self):
        url = "https://example.com/article?gameweek=6&player=123"
        await self.process([self.event("First update."), self.event(f"Second update.\n{url}")])
        self.translation.assert_awaited_once_with("First update.\nSecond update.")
        self.namespace["_display_link_url"].assert_awaited_once_with([url])
        parsed = BeautifulSoup(self.send.await_args.args[0], "html.parser")
        self.assertNotIn(url, parsed.get_text())
        self.assertEqual(parsed.find("a")["href"], url)

    async def test_model_echoing_resolved_url_is_removed_from_caption(self):
        source_url = "https://t.co/example"
        resolved_url = "https://example.com/article?a=1&b=2"
        self.translation.return_value = f"خبر کوتاه\n{resolved_url}"
        self.namespace["_display_link_url"].side_effect = None
        self.namespace["_display_link_url"].return_value = resolved_url
        await self.process([self.event(f"Short update.\n{source_url}")])
        parsed = BeautifulSoup(self.send.await_args.args[0], "html.parser")
        self.assertNotIn(source_url, parsed.get_text())
        self.assertNotIn(resolved_url, parsed.get_text())
        self.assertEqual(parsed.find("a")["href"], resolved_url)

    def test_visible_url_removal_preserves_meaningful_links_and_formatting(self):
        url = "https://example.com/article?a=1&amp;b=2"
        body = f'<blockquote>خبر</blockquote>\n{url}\n<a href="{url}">بیشتر بخوان</a>'
        cleaned = self.namespace["_strip_visible_urls"](body, html=True)
        parsed = BeautifulSoup(cleaned, "html.parser")
        self.assertEqual(parsed.find("blockquote").get_text(), "خبر")
        self.assertEqual(parsed.find("a")["href"], "https://example.com/article?a=1&b=2")
        self.assertIn("بیشتر بخوان", parsed.get_text())
        self.assertNotIn("https://", parsed.get_text())

    def test_link_using_url_as_its_label_leaves_no_empty_anchor(self):
        url = "https://example.com/article?a=1&amp;b=2"
        cleaned = self.namespace["_strip_visible_urls"](f'<a href="{url}">{url}</a>', html=True)
        self.assertEqual(cleaned, "")

    def test_caption_containing_only_printed_url_keeps_footer_link(self):
        url = "https://example.com/article"
        caption = self.namespace["_build_caption"](url, link_url=url, html=True)
        self.assertEqual(caption, f'<a href="{url}">لینک</a>\n\n@EPL_Fantasy | ✨AI')

    def test_article_summary_hides_raw_url_and_keeps_telegraph_link(self):
        caption = self.format_telegraph("عنوان", "خلاصه\nhttps://example.com/article?a=1&amp;b=2", "https://telegra.ph/article")
        parsed = BeautifulSoup(caption, "html.parser")
        self.assertNotIn("https://", parsed.get_text())
        self.assertIn("خلاصه", parsed.get_text())
        self.assertEqual(parsed.find("a")["href"], "https://telegra.ph/article")

    def test_short_article_body_hides_raw_url_and_keeps_source_link(self):
        url = "https://example.com/article?a=1&b=2"
        caption = self.namespace["_format_short_article_post"]("عنوان", f"خبر\n{url}", url)
        parsed = BeautifulSoup(caption, "html.parser")
        self.assertNotIn("https://", parsed.get_text())
        self.assertIn("خبر", parsed.get_text())
        self.assertEqual(parsed.find("a")["href"], url)

    async def test_plain_translation_failure_does_not_publish_an_article(self):
        self.translation.side_effect = TranslationError("Unavailable")
        await self.process([self.event("Short update.")])
        self.article_translation.assert_not_awaited()
        self.publish.assert_not_called()
        self.send.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_automatic_only_source_does_not_enter_translation_buffer(self):
        await self.namespace["handle_new_message"](self.event("Short update."), automatic_only=True)
        self.assertEqual(self.namespace["_chunk_buffer"], {})
        self.translation.assert_not_awaited()
        self.article_translation.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
