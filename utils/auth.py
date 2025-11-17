import os
from typing import Optional
from patchright.async_api import (
    async_playwright,
    Page,
    Browser,
    BrowserContext,
    Playwright,
)
import pyotp
from dotenv import load_dotenv


class USTCAuth:
    """Context manager for USTC authentication and page access"""

    def __init__(self, headless: bool = True, proxy: Optional[dict] = None):
        self.headless = headless
        self.proxy = proxy
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        load_dotenv()
        self.username = os.getenv("USTC_PASSPORT_USERNAME", "")
        self.password = os.getenv("USTC_PASSPORT_PASSWORD", "")
        self.totp_url = os.getenv("USTC_PASSPORT_TOTP_URL", "")

        if os.getenv("HTTP_PROXY_URL"):
            self.proxy = {
                "server": os.getenv("HTTP_PROXY_URL", ""),
                "username": os.getenv("HTTP_PROXY_USERNAME", ""),
                "password": os.getenv("HTTP_PROXY_PASSWORD", ""),
                "bypass": "jw.ustc.edu.cn",
            }

        if self.totp_url:
            self.totp = pyotp.parse_uri(self.totp_url)
        else:
            self.totp = None

    async def __aenter__(self) -> Page:
        """Initialize browser and perform login"""
        # Start playwright
        self.playwright = await async_playwright().__aenter__()

        # Launch browser
        launch_args = {
            "headless": self.headless,
            "args": ["--disable-http2", "--disable-quic"],
        }
        self.browser = await self.playwright.chromium.launch(**launch_args)

        # Create context
        context_args = {}
        if self.proxy:
            context_args["proxy"] = self.proxy
        context_args["locale"] = "zh-CN"
        self.context = await self.browser.new_context(**context_args)
        await self.context.clear_cookies()

        # Create page
        self.page = await self.context.new_page()

        # Perform login
        await self._login()

        return self.page

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup browser resources"""
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def _login(self):
        """Perform USTC login sequence"""
        if not self.page:
            raise RuntimeError("Page not initialized")

        # Login to id.ustc.edu.cn
        await self.page.goto(
            "https://id.ustc.edu.cn", wait_until="networkidle", timeout=0
        )
        await self.page.fill('input[name="username"]', self.username)
        await self.page.fill('input[type="password"]', self.password)
        await self.page.click('button[id="submitBtn"]', timeout=0)
        await self.page.wait_for_timeout(10 * 1000)
        await self.page.wait_for_load_state("networkidle")

        if self.totp:
            await self.page.click("div.ant-tabs-tab:nth-of-type(2)")
            totp_code = self.totp.now()  # type: ignore
            await self.page.fill("input.ant-input", totp_code)
            await self.page.click('button[type="submit"]', timeout=0)
            await self.page.wait_for_timeout(10 * 1000)
            await self.page.wait_for_load_state("networkidle")

        # Login to catalog.ustc.edu.cn
        await self.page.goto(
            "https://passport.ustc.edu.cn/login?service=https://catalog.ustc.edu.cn/ustc_cas_login?next=https://catalog.ustc.edu.cn/",
            wait_until="networkidle",
            timeout=0,
        )

        # Login to jw.ustc.edu.cn
        await self.page.goto(
            "https://passport.ustc.edu.cn/login?service=https%3A%2F%2Fjw.ustc.edu.cn%2Fucas-sso%2Flogin",
            wait_until="networkidle",
            timeout=0,
        )
