import os
import logging
from typing import Optional, Any, Dict
from pathlib import Path

from pyotp import TOTP, parse_uri
from dotenv import load_dotenv
from patchright.async_api import (
    async_playwright,
    Page,
    Browser,
    BrowserContext,
    Playwright,
)

from utils.tools import save_json, cache_dir_from_url


class RequestSession:
    def __init__(
        self,
        page: Page,
        timeout_ms: int = 10 * 60 * 1000,
        fail_on_status_code: bool = True,
        max_retries: int = 100,
    ):
        self.page = page
        self.timeout_ms = timeout_ms
        self.fail_on_status_code = fail_on_status_code
        self.max_retries = max_retries
        self.logger = logging.getLogger(__name__)

    async def get(self, url: str, **kwargs: Any):
        params: Dict[str, Any] = {
            "url": url,
            "timeout": kwargs.get("timeout", self.timeout_ms),
            "fail_on_status_code": kwargs.get(
                "fail_on_status_code", self.fail_on_status_code
            ),
            "max_retries": kwargs.get("max_retries", self.max_retries),
        }

        self.logger.info(f"GET {url}")
        r = await self.page.request.get(**params)
        self.logger.info(f"GET {url} done")

        return r

    async def post(self, url: str, data: Any = None, **kwargs: Any):
        params: Dict[str, Any] = {
            "url": url,
            "data": data,
            "timeout": kwargs.get("timeout", self.timeout_ms),
            "fail_on_status_code": kwargs.get(
                "fail_on_status_code", self.fail_on_status_code
            ),
            "max_retries": kwargs.get("max_retries", self.max_retries),
        }

        self.logger.info(f"POST {url}")
        r = await self.page.request.post(**params)
        self.logger.info(f"POST {url} done")

        return r

    async def get_json(self, url: str, **kwargs: Any):
        r = await self.get(url, **kwargs)

        j = await r.json()

        path = Path(f"{cache_dir_from_url(url)}.json")
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        save_json(j, path)

        self.logger.info(f"cache write {path}")

        return j

    async def post_json(self, url: str, data: Any = None, **kwargs: Any):
        r = await self.post(url, data=data, **kwargs)
        return await r.json()


class USTCSession:
    """Context manager for USTC authentication and returns a RequestSession"""

    def __init__(self, headless: bool = True, proxy: Optional[dict] = None):
        self.headless = headless
        self.proxy = proxy
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.totp: Optional[TOTP] = None

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
            self.totp = parse_uri(self.totp_url)  # type: ignore

    async def __aenter__(self) -> RequestSession:
        """Initialize browser, perform login and return RequestSession"""
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

        return RequestSession(self.page)

    async def __aexit__(self, exc_type, exc, tb):
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
        # screenshot_dir = Path(__file__).parent.parent / "build" / "screenshots"
        # screenshot_dir.mkdir(parents=True, exist_ok=True)

        if not self.page:
            raise RuntimeError("Page not initialized")

        # Login to id.ustc.edu.cn
        await self.page.goto(
            "https://id.ustc.edu.cn",
            wait_until="networkidle",
            timeout=0,
        )
        await self.page.fill(
            'input[name="username"]:not([type="hidden"])',
            strict=True,
            value=self.username,
        )
        await self.page.fill(
            'input[type="password"]:not([type="hidden"])',
            strict=True,
            value=self.password,
        )
        # await self.page.screenshot(path=screenshot_dir / "before_login_submit.png")
        await self.page.click('button[id="submitBtn"]', strict=True)
        await self.page.wait_for_timeout(10 * 1000)
        await self.page.wait_for_load_state("networkidle")
        # await self.page.screenshot(path=screenshot_dir / "after_login_submit.png")

        if self.totp:
            await self.page.click("div.ant-tabs-tab:nth-of-type(2)", strict=True)
            totp_code = self.totp.now()
            await self.page.fill("input.ant-input", strict=True, value=totp_code)
            # await self.page.screenshot(path=screenshot_dir / "before_totp_submit.png")
            await self.page.click('button[type="submit"]', strict=True)
            await self.page.wait_for_timeout(10 * 1000)
            await self.page.wait_for_load_state("networkidle")
            # await self.page.screenshot(path=screenshot_dir / "after_totp_submit.png")

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
