import asyncio
import logging
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Global timeout for any single browser operation (seconds)
OP_TIMEOUT = 15
# Global timeout for an entire page session (seconds)
PAGE_TIMEOUT = 45


async def _with_timeout(coro, timeout: float = OP_TIMEOUT, default=None):
    """Wrap any async operation with a timeout, returning default on failure."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Operation timed out after %ds", timeout)
        return default
    except Exception as e:
        logger.warning("Operation failed: %s", e)
        return default


class BrowserManager:
    def __init__(self, headless: bool = True, slow_mo: int = 50):
        self.headless = headless
        self.slow_mo = slow_mo
        self._playwright = None
        self._browser = None
        self._context = None
        self._initialized = False

    async def init(self):
        if self._initialized:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ]
        )
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="he-IL",
            timezone_id="Asia/Jerusalem",
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self._initialized = True
        logger.info("Browser initialized (headless=%s)", self.headless)

    async def new_page(self) -> Page:
        if not self._initialized:
            await self.init()
        page = await self._context.new_page()
        page.set_default_timeout(OP_TIMEOUT * 1000)
        page.set_default_navigation_timeout(OP_TIMEOUT * 1000)
        return page

    async def navigate(self, page: Page, url: str, timeout: int = 15000) -> bool:
        try:
            await page.goto(url, wait_until="commit", timeout=timeout)
            await asyncio.sleep(1)
            return True
        except Exception as e:
            logger.warning("Navigation failed for %s: %s", url, e)
            return False

    async def safe_get_text(self, page: Page) -> str:
        result = await _with_timeout(page.inner_text("body"), timeout=10, default="")
        return result or ""

    async def find_link_by_text(self, page: Page, keywords: list[str]) -> Optional[str]:
        async def _search():
            links = await page.query_selector_all("a[href]")
            for link in links:
                try:
                    text = (await link.inner_text()).lower().strip()
                    href = await link.get_attribute("href") or ""
                    if any(kw in (text + " " + href.lower()) for kw in keywords):
                        return href
                except Exception:
                    continue
            return None
        return await _with_timeout(_search(), timeout=OP_TIMEOUT, default=None)

    async def find_file_input(self, page: Page):
        return await _with_timeout(
            page.query_selector('input[type="file"]'),
            timeout=5, default=None
        )

    async def click_button(self, page: Page, text: str) -> bool:
        async def _try_click():
            try:
                await page.get_by_role("button", name=text).first.click(timeout=3000)
                return True
            except Exception:
                try:
                    await page.locator(f"text={text}").first.click(timeout=3000)
                    return True
                except Exception:
                    return False
        result = await _with_timeout(_try_click(), timeout=8, default=False)
        return result or False

    async def safe_close_page(self, page: Page):
        """Close a page safely, even if it's hung."""
        try:
            await asyncio.wait_for(page.close(), timeout=5)
        except Exception:
            logger.warning("Page close timed out, forcing...")
            try:
                await asyncio.wait_for(page.goto("about:blank", timeout=2000), timeout=3)
                await asyncio.wait_for(page.close(), timeout=3)
            except Exception:
                logger.warning("Force close also failed, page leaked")

    async def close(self):
        try:
            if self._context:
                await asyncio.wait_for(self._context.close(), timeout=10)
        except Exception:
            logger.warning("Context close timed out")
        try:
            if self._browser:
                await asyncio.wait_for(self._browser.close(), timeout=10)
        except Exception:
            logger.warning("Browser close timed out")
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._initialized = False