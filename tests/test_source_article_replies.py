"""Persist source links and attach articles to live channel message IDs."""
import ast
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teleadmin_project"))
import database as db
import runtime_config
import source_articles


URL = "https://premierleague.app.link/e/mXNcn2yv46b"
TARGET = "@EPL_Fantasy"
SLOT = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class SourceArticleReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        replacements = [
            patch.object(db, "DB_PATH", Path(directory.name) / "fpl.db"),
            patch.object(runtime_config, "DB_PATH", Path(directory.name) / "runtime.db"),
            patch.object(runtime_config, "_env_applied", True),
            patch.object(source_articles, "logger"),
        ]
        connections = []
        for module in (db, runtime_config):
            original = module._connect

            def tracked_connect(original=original):
                connection = original()
                connections.append(connection)
                return connection

            replacements.append(patch.object(module, "_connect", side_effect=tracked_connect))
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(lambda: [connection.close() for connection in connections])
        db.init_db()
        runtime_config.enqueue_source_article(123, 456, URL)
        self.mapping(scheduled=True)
        self.client = SimpleNamespace(get_messages=AsyncMock(return_value=[SimpleNamespace(id=10)]))
        self.history([])
        self.send = AsyncMock()

        async def import_article(url, *, deliver_post):
            await deliver_post("Translated article", file_path="photo.png")
            return True

        self.importer = AsyncMock(side_effect=import_article)
        self.worker = source_articles.SourceArticles(self.client, self.importer, self.send)

    def mapping(self, scheduled, source_msg=456, target_id=10):
        db.store_message_mapping(
            123, source_msg, target_id, is_scheduled=scheduled, target_channel=TARGET,
            target_text="Original translated post", scheduled_at=SLOT.isoformat(),
        )

    def history(self, messages):
        async def iterate(*args, **kwargs):
            for message in messages:
                yield message
        self.client.iter_messages = Mock(side_effect=iterate)

    def bot_functions(self, names, **namespace):
        path = Path(__file__).resolve().parents[1] / "teleadmin_project" / "bot.py"
        nodes = [node for node in ast.parse(path.read_text()).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        namespace.update(db=db, runtime_config=runtime_config, logger=Mock())
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
        return namespace

    async def test_pending_original_waits_without_translating_article(self):
        await self.worker.poll_once()
        self.client.get_messages.assert_awaited_once_with(TARGET, limit=100, scheduled=True)
        self.importer.assert_not_awaited()
        self.assertEqual(runtime_config.pending_source_articles()[0]["state"], "pending")

    async def test_waiting_articles_share_one_scheduled_queue_read_per_target(self):
        self.mapping(scheduled=True, source_msg=457, target_id=11)
        runtime_config.enqueue_source_article(123, 457, "https://example.com/next")
        self.client.get_messages.return_value = [SimpleNamespace(id=10), SimpleNamespace(id=11)]
        await self.worker.poll_once()
        self.client.get_messages.assert_awaited_once()
        self.importer.assert_not_awaited()

    async def test_publication_update_uses_live_id_and_does_not_replace_parent_mapping(self):
        ns = self.bot_functions(
            {"_handle_scheduled_publication"}, _target_channel=lambda: TARGET,
            client=SimpleNamespace(get_input_entity=AsyncMock(return_value=SimpleNamespace(channel_id=99))),
        )
        await ns["_handle_scheduled_publication"](SimpleNamespace(
            peer=SimpleNamespace(channel_id=99), messages=[10], sent_messages=[200],
        ))
        # Reconstruct the worker to exercise restart persistence as well.
        await source_articles.SourceArticles(self.client, self.importer, self.send).poll_once()
        self.importer.assert_awaited_once()
        self.send.assert_awaited_once_with(TARGET, 200, "Translated article", file_path="photo.png")
        self.assertEqual(db.lookup_target_msg(123, 456), 200)
        self.assertEqual(runtime_config.pending_source_articles(), [])
        await self.worker.poll_once()
        self.importer.assert_awaited_once()

    async def test_offline_publication_recovers_only_unique_exact_post(self):
        self.client.get_messages.return_value = []
        self.history([SimpleNamespace(id=200, date=SLOT, message="Original translated post")])
        await self.worker.poll_once()
        self.send.assert_awaited_once_with(TARGET, 200, "Translated article", file_path="photo.png")
        self.assertFalse(db.lookup_message_mapping(123, 456)["is_scheduled"])

    async def test_deleted_scheduled_post_or_ambiguous_history_does_not_reply_elsewhere(self):
        self.client.get_messages.return_value = []
        for history in (
            [],
            [SimpleNamespace(id=199, date=SLOT, message="Another post")],
            [SimpleNamespace(id=i, date=SLOT, message="Original translated post") for i in (199, 200)],
        ):
            self.history(history)
            await self.worker.poll_once()
        self.importer.assert_not_awaited()
        self.assertTrue(db.lookup_message_mapping(123, 456)["is_scheduled"])

    async def test_failure_retries_after_restart_and_does_not_block_next_article(self):
        self.mapping(scheduled=False, target_id=200)
        self.mapping(scheduled=False, source_msg=457, target_id=201)
        runtime_config.enqueue_source_article(123, 457, "https://example.com/next")
        original_import = self.importer.side_effect

        async def first_failure(url, **kwargs):
            if url == URL:
                raise RuntimeError("Photo API unavailable")
            return await original_import(url, **kwargs)

        self.importer.side_effect = first_failure
        await self.worker.poll_once()
        failed = runtime_config.pending_source_articles()
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["state"], "failed")
        self.assertIn("Photo API", failed[0]["last_error"])
        self.send.assert_awaited_once_with(TARGET, 201, "Translated article", file_path="photo.png")
        self.importer.reset_mock()
        await self.worker.poll_once()
        self.importer.assert_not_awaited()  # Back off rather than retry every tick.
        with runtime_config._connect() as connection:
            connection.execute("UPDATE source_article_replies SET last_attempt='2026-01-01T00:00:00+00:00'")
        self.importer.side_effect = original_import
        await source_articles.SourceArticles(self.client, self.importer, self.send).poll_once()
        self.assertEqual(runtime_config.pending_source_articles(), [])
        self.assertEqual(self.send.await_args.args[1], 200)

    async def test_source_detection_records_all_chunk_links_once(self):
        direct_import = AsyncMock()
        ns = self.bot_functions({"_maybe_post_article"}, _publish_article_from_url=direct_import)
        event = SimpleNamespace(chat_id=123, message=SimpleNamespace(id=456))
        for _ in range(2):
            await ns["_maybe_post_article"]("source", event, urls=[URL, URL, "https://example.com/next"])
        self.assertEqual(len(runtime_config.pending_source_articles()), 2)
        direct_import.assert_not_awaited()

    async def test_reply_delivery_uses_review_queue_without_source_event(self):
        send = AsyncMock()
        ns = self.bot_functions({"_send_source_article_reply"}, _send_to_target=send)
        await ns["_send_source_article_reply"](TARGET, 200, "Article caption", file_path="photo.png")
        send.assert_awaited_once_with(
            "Article caption", file_path="photo.png", queue=True, target_channel=TARGET, reply_to=200,
        )

    def test_legacy_message_map_migration_preserves_existing_ids(self):
        with db._connect() as connection:
            connection.execute("DROP TABLE message_map")
            connection.execute("CREATE TABLE message_map (source_chat_id INTEGER, source_msg_id INTEGER, target_msg_id INTEGER, PRIMARY KEY(source_chat_id, source_msg_id))")
            connection.execute("INSERT INTO message_map VALUES(123,456,200)")
        db.init_db()
        db.init_db()
        row = db.lookup_message_mapping(123, 456)
        self.assertEqual(row["target_msg_id"], 200)
        self.assertFalse(row["is_scheduled"])
        self.assertEqual(row["target_channel"], "")

    def test_promotion_only_changes_the_matching_target_scheduled_id(self):
        db.store_message_mapping(124, 456, 10, is_scheduled=True, target_channel="@Other")
        db.promote_scheduled_mapping(TARGET, 10, 200)
        self.assertEqual(db.lookup_target_msg(123, 456), 200)
        self.assertEqual(db.lookup_target_msg(124, 456), 10)

    def test_sender_saves_rendered_caption_and_scheduled_metadata(self):
        ns = self.bot_functions({"_save_mapping"})
        event = SimpleNamespace(chat_id=123, message=SimpleNamespace(id=456))
        message = SimpleNamespace(id=10, message="Rendered caption")
        ns["_save_mapping"](event, message, TARGET, SLOT)
        saved = db.lookup_message_mapping(123, 456)
        self.assertEqual(saved["target_text"], "Rendered caption")
        self.assertTrue(saved["is_scheduled"])
        self.assertEqual(saved["scheduled_at"], SLOT.isoformat())


if __name__ == "__main__":
    unittest.main()
