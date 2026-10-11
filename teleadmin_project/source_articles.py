"""Reply to translated source posts using published, never scheduled, IDs."""
import asyncio
import logging
from datetime import datetime, timedelta

import database as db
import runtime_config

logger = logging.getLogger(__name__)


class SourceArticles:
    def __init__(self, client, import_article, send_reply):
        self.client = client
        self.import_article = import_article
        self.send_reply = send_reply

    async def _published_parent(self, mapping, queues):
        if not mapping["is_scheduled"]:
            return mapping["target_msg_id"]
        target = mapping["target_channel"]
        # Telethon's ids= iterator ignores scheduled=True and queries live
        # history. Inspect the actual scheduled queue instead.
        if target not in queues:
            queues[target] = await self.client.get_messages(target, limit=100, scheduled=True)
        pending = queues[target]
        if any(message.id == mapping["target_msg_id"] for message in pending):
            return None
        # A publication update may have happened while the bot was offline.
        # Recover only a unique exact caption near its original scheduled slot.
        if not mapping["scheduled_at"] or not mapping["target_text"]:
            return None
        scheduled = datetime.fromisoformat(mapping["scheduled_at"])
        matches = []
        async for message in self.client.iter_messages(target, limit=200, offset_date=scheduled + timedelta(minutes=10)):
            if message.date < scheduled - timedelta(minutes=10):
                break
            if message.message == mapping["target_text"]:
                matches.append(message.id)
        if len(matches) != 1:
            return None
        db.promote_scheduled_mapping(target, mapping["target_msg_id"], matches[0])
        return matches[0]

    async def poll_once(self):
        queues = {}
        for row in runtime_config.pending_source_articles():
            try:
                mapping = db.lookup_message_mapping(row["source_chat_id"], row["source_msg_id"])
                if not mapping or not mapping["target_channel"]:
                    continue
                parent_id = await self._published_parent(mapping, queues)
                if parent_id is None or not runtime_config.claim_source_article(row):
                    continue

                async def deliver(caption, *, file_path=None):
                    await self.send_reply(mapping["target_channel"], parent_id, caption, file_path=file_path)

                imported = await self.import_article(row["url"], deliver_post=deliver)
                # Unreadable/non-article links are ignored, as in the original
                # source post-processing path. Exceptions remain retryable.
                runtime_config.finish_source_article(row, success=True)
                logger.info("Source article %s: %s", row["url"], "queued as reply" if imported else "not an article")
            except Exception as exc:
                runtime_config.finish_source_article(row, success=False, error=str(exc))
                logger.exception("Could not prepare source article reply for %s", row["url"])

    async def run(self):
        while True:
            try:
                await self.poll_once()
            except Exception:
                logger.exception("Source article reply cycle failed")
            await asyncio.sleep(30)
