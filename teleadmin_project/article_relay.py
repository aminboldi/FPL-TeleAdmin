"""Publish an article in its author's channel, then forward that actual post."""
import asyncio
import logging

from telethon.errors import FloodWaitError

import runtime_config

logger = logging.getLogger(__name__)


class ArticleRelay:
    def __init__(self, client, source_channel, target_channel, import_article, send_post):
        self.client = client
        self.source_channel = source_channel
        self.target_channel = target_channel
        self.import_article = import_article
        self.send_post = send_post
        self._lock = asyncio.Lock()

    async def _forward(self, relay):
        if relay["forwarded"]:
            return
        try:
            forwarded = await self.client.forward_messages(
                relay["target_channel"], relay["source_message_id"],
                from_peer=relay["source_channel"],
            )
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
            forwarded = await self.client.forward_messages(
                relay["target_channel"], relay["source_message_id"],
                from_peer=relay["source_channel"],
            )
        if forwarded is None:
            raise RuntimeError("Telegram did not return a forwarded article post")
        runtime_config.mark_article_relay_forwarded(relay["url"])
        logger.info("Forwarded %s from %s to %s", relay["url"], relay["source_channel"], relay["target_channel"])

    async def publish(self, url: str) -> bool:
        async with self._lock:
            relay = runtime_config.get_article_relay(url)
            if relay:
                await self._forward(relay)
                return True
            target = self.target_channel()
            if not target:
                raise RuntimeError("TARGET_CHANNEL_ID is not configured")

            async def deliver(text, *, file_path=None):
                # Deliberately bypass the review queue for both channels.
                message = await self.send_post(
                    self.source_channel, text, None, None, file_path=file_path
                )
                runtime_config.save_article_relay(url, self.source_channel, message.id, target)
                await self._forward(runtime_config.get_article_relay(url))

            return await self.import_article(url, deliver_post=deliver)
