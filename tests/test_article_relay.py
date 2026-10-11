"""Persist the origin post and resume only its forward after a failure."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))

import article_monitor
import article_relay
import fplcore
import runtime_config


async def inline_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


class ArticleRelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for replacement in (
            patch.object(runtime_config, "DB_PATH", Path(directory.name) / "runtime.db"),
            patch.object(runtime_config, "_env_applied", True),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        # SQLite's transaction context does not close connections. Retain
        # and close all test connections, including those from existing init().
        connections = []
        connect = runtime_config._connect

        def tracked_connect():
            connection = connect()
            connections.append(connection)
            return connection

        replacement = patch.object(runtime_config, "_connect", side_effect=tracked_connect)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.addCleanup(lambda: [connection.close() for connection in connections])
        self.url = "https://www.fplcore.com/blog/gw5-review"
        self.candidate = article_monitor.Candidate(fplcore.SOURCE_KEY, "Review", self.url, "2026-10-01T12:00:00Z")
        self.client = SimpleNamespace(forward_messages=AsyncMock(return_value=SimpleNamespace(id=200)))
        self.send = AsyncMock(return_value=SimpleNamespace(id=100))

        async def translate(url, *, deliver_post):
            await deliver_post("<b>Translated article</b>", file_path="photo.jpg")
            return True

        self.importer = AsyncMock(side_effect=translate)
        self.relay = self.make_relay()

    def make_relay(self):
        return article_relay.ArticleRelay(
            self.client, "@FPL_ARMO", lambda: "@EPL_Fantasy", self.importer, self.send
        )

    async def test_publish_origin_then_forward_actual_post_without_schedule(self):
        async def forward(*args, **kwargs):
            saved = runtime_config.get_article_relay(self.url)
            self.assertEqual(saved["source_message_id"], 100)
            self.assertFalse(saved["forwarded"])
            return SimpleNamespace(id=200)

        self.client.forward_messages.side_effect = forward
        self.assertTrue(await self.relay.publish(self.url))
        self.send.assert_awaited_once_with("@FPL_ARMO", "<b>Translated article</b>", None, None, file_path="photo.jpg")
        self.client.forward_messages.assert_awaited_once_with("@EPL_Fantasy", 100, from_peer="@FPL_ARMO")
        self.assertTrue(runtime_config.get_article_relay(self.url)["forwarded"])

    async def test_restart_after_failed_forward_reuses_origin_without_translation(self):
        self.client.forward_messages.side_effect = RuntimeError("Telegram temporarily unavailable")
        with self.assertRaises(RuntimeError):
            await self.relay.publish(self.url)
        saved = runtime_config.get_article_relay(self.url)
        self.assertFalse(saved["forwarded"])
        self.client.forward_messages.side_effect = None
        self.assertTrue(await self.make_relay().publish(self.url))
        self.importer.assert_awaited_once()
        self.send.assert_awaited_once()
        self.assertEqual(self.client.forward_messages.await_count, 2)
        self.assertTrue(runtime_config.get_article_relay(self.url)["forwarded"])

    async def test_completed_relay_is_not_posted_or_forwarded_again(self):
        await self.relay.publish(self.url)
        self.assertTrue(await self.make_relay().publish(self.url))
        self.send.assert_awaited_once()
        self.client.forward_messages.assert_awaited_once()

    async def test_failed_origin_send_does_not_forward_or_save_delivery(self):
        self.send.side_effect = RuntimeError("No permission to post")
        with self.assertRaises(RuntimeError):
            await self.relay.publish(self.url)
        self.client.forward_messages.assert_not_awaited()
        self.assertIsNone(runtime_config.get_article_relay(self.url))

    async def test_missing_forward_result_remains_retryable(self):
        self.client.forward_messages.return_value = None
        with self.assertRaisesRegex(RuntimeError, "did not return"):
            await self.relay.publish(self.url)
        self.assertFalse(runtime_config.get_article_relay(self.url)["forwarded"])

    async def test_added_source_seeds_backlog_then_new_article_uses_special_route(self):
        runtime_config.mark_article_monitor_bootstrapped()
        normal = AsyncMock(return_value=True)
        special = AsyncMock(return_value=True)
        other = article_monitor.Candidate("allaboutfpl", "Other", "https://allaboutfpl.com/new")
        with (
            patch.object(article_monitor.asyncio, "to_thread", new=inline_thread),
            patch.object(article_monitor, "discover_candidates", return_value=[self.candidate, other]) as discover,
        ):
            await article_monitor._poll_once(normal, import_fplarmo=special)
            special.assert_not_awaited()
            normal.assert_awaited_once_with(other.url)
            new = article_monitor.Candidate(fplcore.SOURCE_KEY, "New review", self.url + "-new")
            discover.return_value = [self.candidate, new, other]
            await article_monitor._poll_once(normal, import_fplarmo=special)
        special.assert_awaited_once_with(new.url)
        self.assertEqual(normal.await_count, 1)

    async def test_pending_forward_is_retried_when_article_has_left_feed(self):
        runtime_config.mark_article_monitor_bootstrapped()
        runtime_config.mark_article_monitor_bootstrapped(fplcore.SOURCE_KEY)
        runtime_config.claim_article_monitor_candidate(self.candidate.as_dict())
        runtime_config.save_article_relay(self.url, "@FPL_ARMO", 100, "@EPL_Fantasy")
        runtime_config.finish_article_monitor_candidate(self.url, success=False)
        with runtime_config._connect() as connection:
            connection.execute("UPDATE article_monitor_seen SET updated_at='2026-01-01T00:00:00+00:00'")
        normal = AsyncMock()
        with (
            patch.object(article_monitor.asyncio, "to_thread", new=inline_thread),
            patch.object(article_monitor, "discover_candidates", return_value=[]),
        ):
            await article_monitor._poll_once(normal, import_fplarmo=self.relay.publish)
        normal.assert_not_awaited()
        self.importer.assert_not_awaited()
        self.client.forward_messages.assert_awaited_once()
        self.assertEqual(runtime_config.pending_article_relays(), [])
        self.assertFalse(runtime_config.claim_article_monitor_candidate(self.candidate.as_dict()))

    async def test_failed_relay_does_not_suppress_other_sources(self):
        runtime_config.mark_article_monitor_bootstrapped()
        runtime_config.mark_article_monitor_bootstrapped(fplcore.SOURCE_KEY)
        special = AsyncMock(side_effect=RuntimeError("Forward failed"))
        normal = AsyncMock(return_value=True)
        other = article_monitor.Candidate("allaboutfpl", "Other", "https://allaboutfpl.com/new", "2026-10-02")
        with (
            patch.object(article_monitor.asyncio, "to_thread", new=inline_thread),
            patch.object(article_monitor, "discover_candidates", return_value=[self.candidate, other]),
            patch.object(article_monitor, "logger"),
        ):
            await article_monitor._poll_once(normal, import_fplarmo=special)
        special.assert_awaited_once_with(self.url)
        normal.assert_awaited_once_with(other.url)


if __name__ == "__main__":
    unittest.main()
