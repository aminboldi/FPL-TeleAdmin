"""Scout's automatic filter must preserve weekly advice and reject mixed labels."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import article_monitor


def rss(items):
    entries = []
    for index, (title, labels) in enumerate(items):
        categories = "".join(f"<category><![CDATA[{label}]]></category>" for label in labels)
        entries.append(
            f"<item><title>{escape(title)}</title>"
            f"<link>https://www.fantasyfootballscout.co.uk/article-{index}/</link>"
            f"<pubDate>Sun, 11 Oct 2026 12:00:00 +0000</pubDate>{categories}</item>"
        )
    return "<rss><channel>" + "".join(entries) + "</channel></rss>"


class ScoutFilterTests(unittest.TestCase):
    def candidates(self, items, source="fantasyfootballscout"):
        with patch.object(article_monitor, "_fetch", return_value=rss(items)):
            return article_monitor._rss_candidates(source, "https://example.com/feed/")

    def test_weekly_recommendations_remain_eligible(self):
        series = (
            "Scout Picks", "Scout Squad", "Scout Picks - Bus Team",
            "Captain Sensible", "Captain Poll", "Captains", "Watchlist",
            "Spot the Differential", "Chip Strategy", "Fixtures", "Tips + Guides",
        )
        candidates = self.candidates([(label, ["FPL", label]) for label in series])
        self.assertEqual([candidate.title for candidate in candidates], list(series))

    def test_entire_excluded_sections_and_author_children_are_rejected(self):
        for label in (
            "Team News", "Team Reveals", "Pro Pundits", "Pro Pundits - Meerkat",
            "Pro Pundits - Fábio", "Pro Pundits - New Author", "FPL Milanista",
            "Home Page Exclusions", "Dugout Discussion", "Scoreboard",
            " team   NEWS ", "pro pundits - lateriser",
        ):
            with self.subTest(label=label):
                self.assertEqual(self.candidates([("Weekly advice", ["FPL", label])]), [])

    def test_exclusions_win_even_when_weekly_label_comes_first(self):
        for label in ("Team News", "Pro Pundits - Pras", "Team Reveals", "Fantasy EFL"):
            with self.subTest(label=label):
                self.assertEqual(self.candidates([("Picks", ["Scout Picks", "FPL", label])]), [])

    def test_other_games_are_rejected(self):
        for label in (
            "Fantasy EFL", "Fantasy Bundesliga", "Champions League",
            "WSL Fantasy", "FPL Challenge", "FPL Draft", "FanTeam", "SoRare",
            "World Cup Fantasy 2026", "Türkiye Fantasy",
        ):
            with self.subTest(label=label):
                self.assertEqual(self.candidates([("Advice", [label])]), [])

    def test_notes_reports_and_ordinary_tags_are_preserved(self):
        items = [
            ("Team news: injury analysis", ["Scout Notes", "Hemmings injury", "FPL team news"]),
            ("Player report", ["Scout Reports"]),
            ("Arsenal half-term report", ["FPL"]),
            ("New useful series", ["New section"]),
        ]
        self.assertEqual([c.title for c in self.candidates(items)], [title for title, _ in items])

    def test_filter_only_applies_to_scout_automatic_source(self):
        items = [("Team news", ["Team News", "Pro Pundits"])]
        self.assertEqual(len(self.candidates(items, source="allaboutfpl")), 1)

    def test_excluded_entries_do_not_use_candidate_limit(self):
        items = [("Noise", ["Team News"])] * 12 + [("Scout Picks", ["Scout Picks"])]
        self.assertEqual([c.title for c in self.candidates(items)], ["Scout Picks"])

    def test_atom_category_terms_also_obey_filter(self):
        feed = (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            '<title>Team reveal</title><link href="https://example.com/article"/>'
            '<category term="Team Reveals"/></entry></feed>'
        )
        with patch.object(article_monitor, "_fetch", return_value=feed):
            self.assertEqual(article_monitor._rss_candidates("fantasyfootballscout", "https://example.com/feed"), [])


if __name__ == "__main__":
    unittest.main()
