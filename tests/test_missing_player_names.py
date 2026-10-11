"""Name enrichment must preserve curated database spellings and retry failures."""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import database as db
import scheduler
import translate_names


class MissingPlayerNamesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(db.SCHEMA)
        self.conn.executemany(
            "INSERT INTO players (id, first_name, second_name, web_name, team_id, "
            "position_id, now_cost, first_name_fa, second_name_fa, web_name_fa, alias) "
            "VALUES (?, ?, ?, ?, 11, 3, 50, ?, ?, ?, ?)",
            [
                (617, "Mohamed-Ali", "Cho", "Cho", None, None, None, "Manual alias"),
                (572, "Konstantinos", "Tzolakis", "Tzolakis", "نام دستی", "", "نام نمایشی دستی", None),
                (286, "Mohamed", "Belloumi", "Belloumi", "محمد", "بلومی", "بلومی", None),
            ],
        )
        self.conn.commit()
        connections = patch.object(translate_names, "_connect", return_value=self.conn)
        connections.start()
        self.addCleanup(connections.stop)
        self.result = [
            {"id": 572, "first_name_fa": "کنستانتینوس", "second_name_fa": "تزولاکیس", "web_name_fa": "تزولاکیس"},
            {"id": 617, "first_name_fa": "محمد علی", "second_name_fa": "چو", "web_name_fa": "چو"},
        ]

    def rows(self):
        self.conn.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in self.conn.execute("SELECT * FROM players")}

    async def test_only_missing_fields_are_filled_and_aliases_are_preserved(self):
        with patch.object(translate_names, "translate_batch", new=AsyncMock(return_value=self.result)) as call:
            count = await translate_names.fill_missing_names(None, "primary", "fallback")
        self.assertEqual(count, 2)
        self.assertEqual([p["id"] for p in call.await_args.args[2]], [572, 617])
        rows = self.rows()
        self.assertEqual(rows[617]["web_name_fa"], "چو")
        self.assertEqual(rows[617]["alias"], "Manual alias")
        self.assertEqual(rows[572]["first_name_fa"], "نام دستی")
        self.assertEqual(rows[572]["web_name_fa"], "نام نمایشی دستی")
        self.assertEqual(rows[572]["second_name_fa"], "تزولاکیس")
        self.assertEqual(rows[286]["web_name_fa"], "بلومی")
        with patch.object(translate_names, "translate_batch", new=AsyncMock()) as call:
            self.assertEqual(await translate_names.fill_missing_names(None, "primary", "fallback"), 0)
            call.assert_not_awaited()

    async def test_admin_edit_during_translation_wins(self):
        async def translate(*args):
            with self.conn:
                self.conn.execute("UPDATE players SET web_name_fa='انتخاب مدیر' WHERE id=617")
            return self.result
        with patch.object(translate_names, "translate_batch", side_effect=translate):
            await translate_names.fill_missing_names(None, "primary", "fallback")
        self.assertEqual(self.rows()[617]["web_name_fa"], "انتخاب مدیر")

    async def test_failed_translation_is_retryable_without_a_new_player(self):
        with patch.object(translate_names, "translate_batch", side_effect=RuntimeError("Provider unavailable")):
            with self.assertRaises(RuntimeError):
                await translate_names.fill_missing_names(None, "primary", "fallback")
        self.assertIsNone(self.rows()[617]["web_name_fa"])
        with patch.object(translate_names, "translate_batch", new=AsyncMock(return_value=self.result)):
            self.assertEqual(await translate_names.fill_missing_names(None, "primary", "fallback"), 2)

    async def test_invalid_persian_response_is_not_saved(self):
        self.result[1]["web_name_fa"] = "Cho"
        with patch.object(translate_names, "translate_batch", new=AsyncMock(return_value=self.result)):
            with self.assertRaises(ValueError):
                await translate_names.fill_missing_names(None, "primary", "fallback")
        self.assertIsNone(self.rows()[617]["first_name_fa"])
        self.assertEqual(self.rows()[572]["second_name_fa"], "")

    async def test_scheduler_enrichment_failure_is_isolated_and_reloads_names(self):
        async def run_in_test_thread(function):
            return function()
        with patch.object(scheduler.player_names, "reload") as reload_names, patch.object(
            scheduler.asyncio, "to_thread", side_effect=run_in_test_thread
        ):
            with self.assertLogs(scheduler.logger, level="ERROR"):
                await scheduler._enrich_player_names(AsyncMock(side_effect=RuntimeError("Provider unavailable")))
            reload_names.assert_called_once()


if __name__ == "__main__":
    unittest.main()
