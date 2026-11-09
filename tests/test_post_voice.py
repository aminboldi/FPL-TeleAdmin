"""Offline checks for post tone, summary length and the YouTube creator voice."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import post_format
import translator
import youtube_posts


class CreatorNameTests(unittest.TestCase):
    def test_known_channels_resolve_to_persian_first_names(self):
        cases = {
            "FPL Harry": "هری",
            "FPL Raptor": "راس",
            "FPL Focal": "اسکار",
            "Let's Talk FPL": "اندی",
            "Lets Talk FPL": "اندی",
            "FPL Mate": "دن",
            "fpl harry": "هری",
        }
        for title, name in cases.items():
            with self.subTest(title=title):
                self.assertEqual(youtube_posts.creator_name(title), name)

    def test_unknown_channels_get_no_name(self):
        for title in ("FPL Someone Else", "", "Harry Kane Highlights", "FPL"):
            with self.subTest(title=title):
                self.assertEqual(youtube_posts.creator_name(title), "")


class SummaryLengthTests(unittest.TestCase):
    def test_summary_grows_with_the_article(self):
        short = translator._summary_plan("کلمه " * 300)[1]
        medium = translator._summary_plan("کلمه " * 900)[1]
        long = translator._summary_plan("کلمه " * 2500)[1]
        self.assertLess(short, medium)
        self.assertLess(medium, long)

    def test_clean_summary_respects_the_tier_cap(self):
        sentence = "این یک جمله کامل برای آزمایش است. "
        for limit in (250, 450, 650):
            with self.subTest(limit=limit):
                cleaned = translator.Translator._clean_summary(sentence * 40, limit)
                self.assertLessEqual(len(cleaned), limit + 1)

    def test_longest_youtube_caption_fits_telegram_media_limit(self):
        caption = post_format._format_youtube_telegraph_post(
            "ع" * 100,
            "س" * translator._LONG_SUMMARY[1],
            "https://telegra.ph/Some-Long-Persian-Article-Path-11-09",
            "https://www.youtube.com/watch?v=abcdefghijk",
            creator="اسکار",
        )
        self.assertLessEqual(post_format._caption_length(caption), post_format._MEDIA_CAPTION_LIMIT)


class PromptTests(unittest.TestCase):
    def test_every_reader_facing_prompt_gets_the_tone_guide(self):
        for prompt in (
            translator.TRANSLATION_PROMPT,
            translator.ARTICLE_PROMPT,
            translator.SUMMARY_PROMPT,
        ):
            self.assertIn("{tone}", prompt)
            rendered = translator._render(prompt, tone=translator.TONE_PROMPT)
            self.assertNotIn("{tone}", rendered)
            self.assertIn("می‌باشد", rendered)

    def test_both_translating_prompts_get_the_one_glossary(self):
        for prompt in (translator.TRANSLATION_PROMPT, translator.ARTICLE_PROMPT):
            self.assertIn("{glossary}", prompt)
            self.assertNotIn("Return: امتیاز", prompt)
            rendered = translator._render(prompt, glossary=translator.GLOSSARY_PROMPT)
            self.assertIn("مشارکت در گل", rendered)
            self.assertIn("WXH: رکسهم", rendered)

    def test_glossary_never_maps_goal_involvement_to_points(self):
        for line in translator.GLOSSARY_PROMPT.splitlines():
            if line.startswith("- Return /"):
                self.assertIn("مشارکت در گل", line)
                self.assertNotIn("→ امتیاز", line)
                break
        else:
            self.fail("glossary has no Return entry")

    def summary_prompt(self, speaker):
        client = translator.Translator("key", "model", "fallback", google_api_key="g")
        call = AsyncMock(return_value="خلاصه")
        with patch.object(client, "_call_summary_model", call):
            asyncio.run(client.summarize_article("<p>متن مقاله</p>", speaker=speaker))
        return call.call_args.args[2]

    def test_creator_summary_is_first_person(self):
        prompt = self.summary_prompt("هری")
        self.assertIn("This is a video by هری", prompt)
        self.assertIn("first person", prompt)
        self.assertNotIn("{voice}", prompt)
        self.assertNotIn("{length}", prompt)

    def test_other_summaries_keep_the_editor_voice(self):
        prompt = self.summary_prompt("")
        self.assertIn(translator._EDITOR_VOICE, prompt)
        self.assertNotIn("first person", prompt)


class YouTubeBylineTests(unittest.TestCase):
    def test_byline_sits_under_the_title_only_for_known_creators(self):
        named = post_format._format_youtube_inline_post("عنوان", "متن", "https://youtu.be/x", creator="دن")
        self.assertTrue(named.startswith("<b>عنوان</b>\n🎙 دن\n\n"))
        unnamed = post_format._format_youtube_inline_post("عنوان", "متن", "https://youtu.be/x")
        self.assertNotIn("🎙", unnamed)


if __name__ == "__main__":
    unittest.main()
