"""Offline regression checks for fixture-scoped game alerts.

Run from the repository root: teleadmin_project/.venv/bin/python -m unittest discover -s tests
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import alerts
import database as db


class TeamAliasAlertsTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            conn.executescript(db.SCHEMA)
            conn.executemany(
                "INSERT INTO teams (id, name, short_name, name_fa, short_name_fa) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "Man Utd", "MUN", "منچستر یونایتد", "منچستر یونایتد"),
                    (2, "Spurs", "TOT", "تاتنهام هاتسپر", "تاتنهام"),
                    (3, "Nott'm Forest", "NFO", "ناتینگهام فارست", "فارست"),
                    (4, "Man City", "MCI", "منچستر سیتی", "منچستر سیتی"),
                    (5, "Brighton", "BHA", "برایتون", "برایتون"),
                    (6, "Newcastle", "NEW", "نیوکاسل", "نیوکاسل"),
                    (7, "West Ham", "WHU", "وست هم", "وست هم"),
                    (8, "Wolves", "WOL", "ولوز", "ولوز"),
                    (9, "Leeds", "LEE", "لیدز یونایتد", "لیدز"),
                    (10, "Hull City", "HUL", "هال سیتی", "هال"),
                ],
            )
            conn.execute("INSERT INTO positions (id, singular_name) VALUES (3, 'Midfielder')")
            conn.execute("INSERT INTO positions (id, singular_name) VALUES (1, 'Goalkeeper')")
            conn.executemany(
                "INSERT INTO players (id, first_name, second_name, web_name, team_id, "
                "position_id, now_cost, web_name_fa, total_points) VALUES (?, ?, ?, ?, ?, 3, ?, ?, ?)",
                [
                    (1, "Bryan", "Mbeumo", "Mbeumo", 1, 80, "امبومو", 10),
                    (2, "Matheus", "Cunha", "Cunha", 1, 70, "کونیا", 10),
                    (3, "Chris", "Wood", "Wood", 3, 60, "وود", 10),
                    # A high-scoring namesake at the other Manchester club
                    # must never win over a player in the actual fixture.
                    (4, "Other", "Mbeumo", "Mbeumo", 4, 100, "بازیکن اشتباه", 999),
                    (5, "Mohamed", "Belloumi", "Belloumi", 10, 50, "بلومی", 10),
                    (6, "Konstantinos", "Tzolakis", "Tzolakis", 10, 45, None, 10),
                    (7, "Callum", "Hudson-Odoi", "Hudson-Odoi", 3, 55, "هادسون اودوی", 10),
                ],
            )
            conn.execute("UPDATE players SET alias='CHO' WHERE id=7")
            conn.execute("UPDATE players SET position_id=1 WHERE id=6")
        connection_patch = patch.object(db, "_connect", return_value=conn)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        expected_warnings = patch.object(alerts, "logger")
        expected_warnings.start()
        self.addCleanup(expected_warnings.stop)

    def format_goal(self, home="Man United", away="Tottenham", player="MBEUMO", hashtag=""):
        source = f"Goal - {player}\nAssist - CUNHA\n\n{home} 1-0 {away} (73 mins)\n\n{hashtag}"
        self.assertTrue(alerts.is_game_alert(source))
        parsed = alerts.parse(source)
        self.assertIsNotNone(parsed)
        return parsed, alerts.format_farsi(parsed)

    def test_reported_manchester_united_goal(self):
        for name in ("Man United", "Manchester United", "Manchester Utd", "Man Utd", "MAN. UNITED"):
            with self.subTest(name=name):
                parsed, text = self.format_goal(home=name)
                self.assertEqual((parsed.home_team_code, parsed.away_team_code), ("MUN", "TOT"))
                self.assertIn("منچستر یونایتد <b>1</b> تاتنهام <b>0</b>", text)
                self.assertIn("⚽ امبومو <b>8.0M</b>", text)
                self.assertIn("🅰️ کونیا <b>7.0M</b>", text)
                self.assertNotIn("بازیکن اشتباه", text)

    def test_forest_spellings_resolve_players_with_or_without_hashtags(self):
        for name in ("Nott'm Forest", "Nottm Forest", "Nottingham Forest", "Forest", "Forrest", "Nottingham Forrest"):
            for hashtag in ("", "#FPL #NFOTOT"):
                with self.subTest(name=name, hashtag=hashtag):
                    parsed, text = self.format_goal(home=name, player="WOOD", hashtag=hashtag)
                    self.assertEqual(parsed.home_team_code, "NFO")
                    self.assertIn("فارست <b>1</b>", text)
                    self.assertIn("⚽ وود <b>6.0M</b>", text)

    def test_other_common_names_and_punctuation(self):
        variants = {
            "mAnChEsTeR  cItY": "MCI",
            "Brighton & Hove Albion": "BHA",
            "Brighton and Hove Albion": "BHA",
            "Newcastle United": "NEW",
            "West Ham United": "WHU",
            "Wolverhampton Wanderers": "WOL",
            "Leeds United": "LEE",
            "Tottenham Hotspur": "TOT",
            "Nott’m Forest": "NFO",
            "منچستر یونایتد": "MUN",
            "mun": "MUN",
        }
        for name, code in variants.items():
            with self.subTest(name=name):
                self.assertEqual(alerts._resolve_team("", name)["short_name"], code)

    def test_valid_hashtag_codes_take_priority(self):
        parsed, text = self.format_goal(home="Unfamiliar name", hashtag="#FPL #MUNTOT")
        self.assertEqual(parsed.home_team_code, "MUN")
        self.assertIn("⚽ امبومو <b>8.0M</b>", text)

    def test_aliases_have_same_dedup_identity(self):
        canonical, _ = self.format_goal(home="Man Utd", away="Spurs", hashtag="#FPL #MUNTOT")
        alternate, _ = self.format_goal(home="Manchester United", away="Tottenham Hotspur")
        self.assertEqual(alerts.dedup_key(canonical), alerts.dedup_key(alternate))

    def test_lineups_keep_their_strict_club_lookup(self):
        parsed = alerts.parse_lineup("LINE-UPS | #MUNTOT\nX MUN: Mbeumo, Cunha\nX TOT: Mbeumo")
        text = alerts.format_lineup(parsed)
        self.assertIn("<blockquote>امبومو <b>8.0M</b></blockquote>", text)
        self.assertIn("<blockquote>کونیا <b>7.0M</b></blockquote>", text)
        self.assertIn("<blockquote>Mbeumo</blockquote>", text)
        self.assertNotIn("بازیکن اشتباه", text)

    def test_hull_short_name_resolves_belloumi_and_keeps_cho_fixture_scoped(self):
        for name in ("Hull", "Hull City", "HUL"):
            with self.subTest(name=name):
                parsed = alerts.parse(
                    f"Goal - CHO\nAssist - BELLOUMI\n\nNewcastle 2-1 {name} (67 mins)"
                )
                text = alerts.format_farsi(parsed)
                self.assertEqual((parsed.home_team_code, parsed.away_team_code), ("NEW", "HUL"))
                self.assertIn("هال <b>1</b>", text)
                self.assertIn("🅰️ بلومی <b>5.0M</b>", text)
                self.assertIn("⚽ CHO", text)
                self.assertNotIn("هادسون اودوی", text)

    def test_tzolakis_without_persian_name_still_resolves_price(self):
        parsed = alerts.parse(
            "Penalty missed - BELLOUMI\nPenalty saved - TZOLAKIS\n\nNewcastle 2-0 Hull (46+2 mins)"
        )
        text = alerts.format_farsi(parsed)
        self.assertEqual(parsed.away_team_code, "HUL")
        self.assertIn("📛 Tzolakis <b>4.5G</b> (مهار پنالتی)", text)

    def test_exact_source_resolves_hulls_cho_after_roster_is_current(self):
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO players (id, first_name, second_name, web_name, team_id, "
                "position_id, now_cost, web_name_fa, total_points) "
                "VALUES (617, 'Mohamed-Ali', 'Cho', 'Cho', 10, 3, 50, 'چو', 10)"
            )
        source = "Goal - CHO\nAssist - BELLOUMI\n\nNewcastle 2-1 Hull (67 mins)\n\n#FPL #NEWHUL"
        self.assertTrue(alerts.is_game_alert(source))
        parsed = alerts.parse(source)
        self.assertEqual((parsed.home_team_code, parsed.away_team_code), ("NEW", "HUL"))
        text = alerts.format_farsi(parsed)
        self.assertIn("⚽ چو <b>5.0M</b>", text)
        self.assertIn("🅰️ بلومی <b>5.0M</b>", text)
        self.assertNotIn("هادسون اودوی", text)

    def test_unrecognized_or_ambiguous_names_do_not_search_all_clubs(self):
        for name in ("United", "City", "Not a club", "", "Manchester"):
            with self.subTest(name=name):
                self.assertIsNone(alerts._resolve_team("", name))
        _, text = self.format_goal(home="Unknown home", away="Unknown away")
        self.assertIn("⚽ MBEUMO", text)
        self.assertNotIn("امبومو", text)
        self.assertNotIn("بازیکن اشتباه", text)

    def test_one_unresolved_team_still_cannot_pick_unrelated_player(self):
        _, text = self.format_goal(home="Unknown home", away="Tottenham")
        self.assertIn("⚽ MBEUMO", text)
        self.assertNotIn("امبومو", text)
        self.assertNotIn("بازیکن اشتباه", text)

    def test_own_goal_resolves_player_in_name_first_format(self):
        # Source feeds write this as "Davis (own goal)", not "own goal (Davis)".
        parsed, text = self.format_goal(player="MBEUMO (own goal)")
        self.assertEqual(parsed.actions[0].type, "own_goal")
        self.assertEqual(parsed.actions[0].player_name, "MBEUMO")
        self.assertIn("⚽ امبومو <b>8.0M</b> (گل بخودی)", text)

    def test_own_goal_also_resolves_name_second_format(self):
        parsed, text = self.format_goal(player="own goal (MBEUMO)")
        self.assertEqual(parsed.actions[0].type, "own_goal")
        self.assertEqual(parsed.actions[0].player_name, "MBEUMO")
        self.assertIn("⚽ امبومو <b>8.0M</b> (گل بخودی)", text)


if __name__ == "__main__":
    unittest.main()
