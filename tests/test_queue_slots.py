"""The half-hour review queue and the nightly price watchlist's slot."""
import asyncio
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import bot_source
import post_queue
import scheduler

IRAN = post_queue.IRAN_TZ


def iran(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=IRAN)


def utc(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.now = iran(9, 13, 12)
        self.scheduled = []
        self.watchlist = []
        clock = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz)

        self.ns = bot_source.load({"_next_queue_slot"}, {
            "client": SimpleNamespace(get_messages=AsyncMock(
                side_effect=lambda *a, **k: [SimpleNamespace(date=d) for d in self.scheduled]
            )),
            "scheduler": SimpleNamespace(reserved_prediction_slots=lambda now: self.watchlist),
            "post_queue": post_queue, "datetime": Clock, "timezone": timezone,
            "timedelta": timedelta, "logger": Mock(),
            "_recent_queue_reservations": {}, "_RESERVATION_GRACE": timedelta(minutes=2),
        })

    def reserve(self):
        return asyncio.run(self.ns["_next_queue_slot"]("@EPL_Fantasy")).astimezone(IRAN)

    def test_a_freed_slot_is_filled_before_later_ones(self):
        # 14:00 was used, then published early by an admin; 14:30 is still queued.
        self.scheduled = [iran(9, 14, 30)]
        self.assertEqual(self.reserve(), iran(9, 14, 0))

    def test_back_to_back_posts_do_not_share_a_slot_before_telegram_lists_them(self):
        self.assertEqual(self.reserve(), iran(9, 14, 0))
        # Telegram has not listed the first one yet.
        self.assertEqual(self.reserve(), iran(9, 14, 30))

    def test_a_deleted_post_frees_its_slot_once_the_grace_period_is_over(self):
        self.assertEqual(self.reserve(), iran(9, 14, 0))
        self.now += timedelta(minutes=5)
        # The 14:00 post was deleted from the scheduled list.
        self.assertEqual(self.reserve(), iran(9, 14, 0))

    def test_the_price_watchlist_slot_is_never_given_to_a_post(self):
        self.now = iran(9, 22, 10)
        self.scheduled = [iran(9, 23, 0)]
        self.watchlist = [iran(9, 23, 30)]
        self.assertEqual(self.reserve(), iran(10, 0, 0))


class WatchlistSlotTests(unittest.TestCase):
    def fixture(self, kickoff, finished=0):
        return {"kickoff_time": kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "finished": finished,
                "home_en": "Arsenal", "away_en": "Hull"}

    def test_23_30_on_a_night_without_football(self):
        self.assertEqual(scheduler.prediction_slot(date(2026, 11, 9), []), iran(9, 23, 30))

    def test_an_earlier_match_that_has_ended_does_not_move_it(self):
        night = [self.fixture(iran(9, 21, 0))]
        self.assertEqual(scheduler.prediction_slot(date(2026, 11, 9), night), iran(9, 23, 30))

    def test_a_match_running_past_23_30_moves_it_after_the_match(self):
        night = [self.fixture(iran(9, 22, 0))]
        self.assertEqual(scheduler.prediction_slot(date(2026, 11, 9), night), iran(10, 0, 0))

    def test_a_match_kicking_off_at_23_30_is_part_of_the_night(self):
        night = [self.fixture(iran(9, 23, 30))]
        self.assertEqual(scheduler.prediction_slot(date(2026, 11, 9), night), iran(10, 1, 30))

    def test_upcoming_watchlists_are_reserved(self):
        with patch.object(scheduler.runtime_config, "get_bool", return_value=True), \
             patch.object(scheduler.livefpl, "get_fixtures", return_value=[]):
            slots = scheduler.reserved_prediction_slots(utc(9, 12))
        self.assertIn(iran(9, 23, 30), slots)
        self.assertIn(iran(10, 23, 30), slots)
        with patch.object(scheduler.runtime_config, "get_bool", return_value=False):
            self.assertEqual(scheduler.reserved_prediction_slots(utc(9, 12)), [])

    def run_check(self, now, night, status):
        client = AsyncMock()
        games = [["Arsenal", "Hull", None, None, status]]
        with patch.object(scheduler, "_night_fixtures", return_value=night), \
             patch.object(scheduler, "_already_posted", return_value=False), \
             patch.object(scheduler, "_mark_posted"), \
             patch.object(scheduler, "_check_actual_price_changes", AsyncMock()), \
             patch.object(scheduler.livefpl, "build_price_changes_text", return_value="watchlist"):
            asyncio.run(scheduler._check_price_post(
                client, "@EPL_Fantasy", now.astimezone(timezone.utc) + scheduler._IRAN_OFFSET, True, games,
            ))
        return client.send_message

    def test_watchlist_waits_for_a_late_match_to_finish(self):
        night = [self.fixture(iran(9, 23, 30))]
        self.run_check(iran(9, 23, 31), night, "Live").assert_not_awaited()
        self.run_check(iran(10, 1, 35), night, "Live").assert_not_awaited()
        self.run_check(iran(10, 1, 35), night, "Done").assert_awaited_once()

    def test_watchlist_goes_out_at_23_30_on_a_quiet_night(self):
        self.run_check(iran(9, 23, 30), [], "Done").assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
