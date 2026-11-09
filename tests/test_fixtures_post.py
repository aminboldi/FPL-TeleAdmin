"""The gameweek fixtures post: its layout and when the scheduler sends it."""
import asyncio
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import database as db
import fixtures_post
import scheduler

TEAMS = [
    "اورتون", "کاونتری", "لیدز", "تاتنهام", "آرسنال", "هال", "فولام", "نیوکاسل",
    "فارست", "منچستر سیتی", "برایتون", "برنتفورد", "پالاس", "لیورپول", "ایپسویچ",
    "بورنموث", "ساندرلند", "چلسی", "منچستر یونایتد", "ویلا",
]
# Gameweek 10 as it was played, in UTC (Iran is UTC+3:30).
GW10 = [
    ("2026-11-06T20:00:00Z", "اورتون", "کاونتری"),
    ("2026-11-07T12:30:00Z", "لیدز", "تاتنهام"),
    ("2026-11-07T15:00:00Z", "آرسنال", "هال"),
    ("2026-11-07T15:00:00Z", "فولام", "نیوکاسل"),
    ("2026-11-07T17:30:00Z", "فارست", "منچستر سیتی"),
    ("2026-11-08T14:00:00Z", "برایتون", "برنتفورد"),
    ("2026-11-08T14:00:00Z", "پالاس", "لیورپول"),
    ("2026-11-08T14:00:00Z", "ایپسویچ", "بورنموث"),
    ("2026-11-08T14:00:00Z", "ساندرلند", "چلسی"),
    ("2026-11-08T16:30:00Z", "منچستر یونایتد", "ویلا"),
]
GW10_DEADLINE = datetime(2026, 11, 6, 18, 30, tzinfo=timezone.utc)

# The layout the admin settled on by hand.
EXPECTED = """⚠️ددلاین هفته 10، جمعه 22:00⚠️

جمعه
اورتون - کاونتری | 23:30

شنبه
لیدز - تاتنهام | 16:00
آرسنال - هال | 18:30
فولام - نیوکاسل | 18:30
فارست - منچستر سیتی | 21:00

یکشنبه
برایتون - برنتفورد | 17:30
پالاس - لیورپول | 17:30
ایپسویچ - بورنموث | 17:30
ساندرلند - چلسی | 17:30
منچستر یونایتد - ویلا | 20:00

@EPL_Fantasy"""


class FixturesPostTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        with self.conn:
            self.conn.executescript(db.SCHEMA)
            self.conn.executemany(
                "INSERT INTO teams (id, name, short_name, name_fa, short_name_fa) VALUES (?, ?, ?, ?, ?)",
                [(i, f"T{i}", f"T{i:02d}", name, name) for i, name in enumerate(TEAMS, 1)],
            )
            self.conn.executemany(
                "INSERT INTO gameweeks (id, name, deadline_time) VALUES (?, ?, ?)",
                [
                    (9, "Gameweek 9", "2026-10-30T18:30:00Z"),
                    (10, "Gameweek 10", GW10_DEADLINE.strftime("%Y-%m-%dT%H:%M:%SZ")),
                ],
            )
            # Gameweek 9's last match: Monday 3 November, 20:00 UTC.
            self.conn.execute(
                "INSERT INTO fixtures (id, gameweek_id, team_h, team_a, finished, kickoff_time) "
                "VALUES (90, 9, 1, 2, 0, '2026-11-02T20:00:00Z')"
            )
            team = {name: i for i, name in enumerate(TEAMS, 1)}
            self.conn.executemany(
                "INSERT INTO fixtures (id, gameweek_id, team_h, team_a, kickoff_time) VALUES (?, 10, ?, ?, ?)",
                [(100 + n, team[home], team[away], kickoff) for n, (kickoff, home, away) in enumerate(GW10)],
            )
        connection = patch.object(db, "_connect", return_value=self.conn)
        connection.start()
        self.addCleanup(connection.stop)

    def gameweek(self, gw_id):
        return db.query_one("SELECT id, name, deadline_time FROM gameweeks WHERE id=?", (gw_id,))

    def test_layout_matches_the_hand_edited_post(self):
        text = fixtures_post.text(self.gameweek(10))
        self.assertEqual(re.sub(r"<[^>]+>", "", text), EXPECTED)
        self.assertTrue(text.startswith("<b>⚠️ددلاین هفته 10، جمعه 22:00⚠️</b>\n\n<b>جمعه</b>\n<blockquote>"))

    def test_not_due_while_the_previous_gameweek_is_still_being_played(self):
        during_last_match = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
        self.assertIsNone(fixtures_post.due_post(during_last_match))

    def test_due_for_72_hours_before_the_deadline_once_it_is_over(self):
        after_last_match = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
        gameweek, send_at = fixtures_post.due_post(after_last_match)
        self.assertEqual(gameweek["id"], 10)
        self.assertEqual(send_at, GW10_DEADLINE - timedelta(hours=72))

    def check(self, now):
        client = AsyncMock()
        with patch.object(scheduler, "_notify_admin", AsyncMock()):
            asyncio.run(scheduler._check_fixtures_post(client, "@EPL_Fantasy", now=now))
        return client.send_file

    def test_scheduler_queues_it_in_telegram_once(self):
        send = self.check(datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc))
        send.assert_awaited_once()
        self.assertEqual(send.call_args.args[1], str(fixtures_post.IMAGE))
        self.assertEqual(send.call_args.kwargs["schedule"], GW10_DEADLINE - timedelta(hours=72))
        # A later tick must not schedule it a second time.
        self.check(datetime(2026, 11, 3, 1, 1, tzinfo=timezone.utc)).assert_not_awaited()

    def test_late_finish_sends_at_once_instead_of_skipping(self):
        send = self.check(datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc))
        send.assert_awaited_once()
        self.assertIsNone(send.call_args.kwargs["schedule"])

    def test_nothing_once_the_deadline_has_passed_and_no_gameweek_follows(self):
        self.check(GW10_DEADLINE + timedelta(minutes=1)).assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
