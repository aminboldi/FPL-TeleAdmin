"""Screenshot web page elements using Playwright."""
import asyncio
import logging
from pathlib import Path
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_browser = None
_lock = asyncio.Lock()


async def _get_browser():
    global _browser
    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True)
    return _browser


async def screenshot_element(url: str, selector: str, output_path: str | None = None) -> bytes | None:
    async with _lock:
        try:
            browser = await _get_browser()
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)

            element = await page.query_selector(selector)
            if not element:
                logger.warning("Selector '%s' not found on %s", selector, url)
                await page.close()
                return None

            if output_path:
                await element.screenshot(path=output_path)
                logger.info("Screenshot saved to %s", output_path)
                await page.close()
                return None

            data = await element.screenshot()
            await page.close()
            return data
        except Exception as e:
            logger.error("Screenshot failed: %s", e)
            return None
