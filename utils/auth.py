import os
import logging
from typing import Optional, Any, Dict
from pathlib import Path

from pyotp import TOTP, parse_uri
from enum import Enum
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
        self.logger = logging.getLogger(__name__)

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
        await self._after_login()

        return RequestSession(self.page)

    async def __aexit__(self, exc_type, exc, tb):
        """Cleanup browser resources"""
        try:
            if self.page:
                await self.page.close()
        except Exception:
            pass
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

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
            timeout=60 * 1000,
        )

        self.logger.info("login state machine start")
        success = await self._run_login_state_machine(max_turns=10)
        if not success:
            self.logger.error("login state machine failed")
            raise RuntimeError("Login failed: state machine did not reach success URL")
        self.logger.info("login state machine success")

    def _success_reached(self) -> bool:
        return bool(self.page and ("cas-success" in self.page.url))

    async def _has_credentials_form(self) -> bool:
        if not self.page:
            return False
        try:
            user = self.page.locator('input[name="username"]:not([type="hidden"])')
            pwd = self.page.locator('input[type="password"]:not([type="hidden"])')
            submit = self.page.locator('button#submitBtn')
            ok = (
                await user.count() > 0
                and await pwd.count() > 0
                and await submit.count() > 0
            )
            self.logger.debug(f"detector credentials_form ok={ok}")
            return ok
        except Exception as e:
            self.logger.debug(f"detector credentials_form error={e}")
            return False

    async def _fill_credentials(self) -> None:
        if not self.page:
            return
        self.logger.info("action fill_credentials")
        await self.page.fill(
            'input[name="username"]:not([type="hidden"])',
            strict=True,
            value=self.username,
            timeout=60 * 1000,
        )
        await self.page.fill(
            'input[type="password"]:not([type="hidden"])',
            strict=True,
            value=self.password,
            timeout=60 * 1000,
        )
        btn = self.page.locator('button#submitBtn')
        try:
            await btn.click(force=True, timeout=120 * 1000)
        except Exception:
            await self.page.press(
                'input[type="password"]:not([type="hidden"])',
                'Enter',
                timeout=30 * 1000,
            )
        try:
            await self.page.wait_for_url("**cas-success**", timeout=60 * 1000)
        except Exception:
            pass

    async def _has_totp_tab(self) -> bool:
        if not self.page or not self.totp:
            return False
        try:
            tab = self.page.locator("div.ant-tabs-tab:nth-of-type(2)")
            ok = await tab.count() > 0
            self.logger.debug(f"detector totp_tab ok={ok}")
            return ok
        except Exception as e:
            self.logger.debug(f"detector totp_tab error={e}")
            return False

    async def _fill_totp(self) -> None:
        if not self.page or not self.totp:
            return
        self.logger.info("action fill_totp")
        await self.page.click("div.ant-tabs-tab:nth-of-type(2)", strict=True, timeout=60 * 1000)
        totp_code = self.totp.now()
        await self.page.fill("input.ant-input", strict=True, value=totp_code)
        await self.page.click('button[type="submit"]', strict=True, timeout=60 * 1000)
        try:
            await self.page.wait_for_url("**cas-success**", timeout=60 * 1000)
        except Exception:
            pass

    class LoginState(Enum):
        CHECK_SUCCESS = "CHECK_SUCCESS"
        FILL_CREDENTIALS = "FILL_CREDENTIALS"
        FILL_TOTP = "FILL_TOTP"
        WAIT = "WAIT"
        DONE = "DONE"
        FAILED = "FAILED"

    async def _run_login_state_machine(self, max_turns: int = 10) -> bool:
        state = self.LoginState.CHECK_SUCCESS
        attempted_credentials = False
        attempted_totp = False
        for i in range(1, max_turns + 1):
            self.logger.info(f"login turn={i} state={state.value}")
            if state == self.LoginState.CHECK_SUCCESS:
                if self._success_reached():
                    self.logger.info("success url reached")
                    state = self.LoginState.DONE
                    break
                has_creds = False
                has_totp = False
                if not attempted_credentials:
                    has_creds = await self._has_credentials_form()
                has_totp = await self._has_totp_tab()
                self.logger.info(f"detectors has_creds={has_creds} has_totp={has_totp}")
                if has_creds:
                    state = self.LoginState.FILL_CREDENTIALS
                elif has_totp:
                    state = self.LoginState.FILL_TOTP
                else:
                    state = self.LoginState.WAIT

            if state == self.LoginState.FILL_CREDENTIALS:
                await self._fill_credentials()
                await self.page.wait_for_load_state("networkidle", timeout=60 * 1000)
                state = self.LoginState.CHECK_SUCCESS
                attempted_credentials = True
                self.logger.info("after credentials submit, returning to check_success")
                continue

            if state == self.LoginState.FILL_TOTP:
                await self._fill_totp()
                await self.page.wait_for_load_state("networkidle", timeout=60 * 1000)
                state = self.LoginState.CHECK_SUCCESS
                attempted_totp = True
                self.logger.info("after totp submit, returning to check_success")
                continue

            if state == self.LoginState.WAIT:
                await self.page.wait_for_timeout(500)
                state = self.LoginState.CHECK_SUCCESS
                self.logger.info("waiting, returning to check_success")
                continue

        ok = self._success_reached()
        self.logger.info(f"login final ok={ok}")
        return ok

    async def _after_login(self):
        """Navigate to services after login"""

        if not self.page:
            raise RuntimeError("Page not initialized")

        # Login to catalog.ustc.edu.cn
        await self.page.goto(
            "https://passport.ustc.edu.cn/login?service=https://catalog.ustc.edu.cn/ustc_cas_login?next=https://catalog.ustc.edu.cn/",
            wait_until="networkidle",
            timeout=60 * 1000,
        )

        # Login to jw.ustc.edu.cn
        await self.page.goto(
            "https://passport.ustc.edu.cn/login?service=https%3A%2F%2Fjw.ustc.edu.cn%2Fucas-sso%2Flogin",
            wait_until="networkidle",
            timeout=60 * 1000,
        )
