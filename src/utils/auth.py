import os
import logging
from typing import Optional, Any, Dict
from pathlib import Path
from contextlib import suppress

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

from .tools import save_json, cache_dir_from_url


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
        self.playwright: Playwright
        self.browser: Browser
        self.context: BrowserContext
        self.page: Page
        self.totp: Optional[TOTP] = None
        self.logger = logging.getLogger(__name__)

        load_dotenv()
        self.timeout_ms = int(os.getenv("USTC_TIMEOUT_MS", "0"))
        self.username = os.getenv("USTC_PASSPORT_USERNAME", "")
        self.password = os.getenv("USTC_PASSPORT_PASSWORD", "")
        if self.username == "" or self.password == "":
            raise ValueError(
                "USTC_PASSPORT_USERNAME and USTC_PASSPORT_PASSWORD must be set in environment variables"
            )
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
        self.playwright = await async_playwright().__aenter__()
        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(locale="zh-CN")
        self.page = await self.context.new_page()

        await self._login()
        await self._after_login()

        return RequestSession(self.page)

    async def __aexit__(self, exc_type, exc, tb):
        """Cleanup browser resources"""
        with suppress(Exception):
            await self.page.close()
        with suppress(Exception):
            await self.context.close()
        with suppress(Exception):
            await self.browser.close()
        with suppress(Exception):
            await self.playwright.stop()

    async def _login(self):
        """Perform USTC login sequence"""
        await self.page.goto(
            "https://id.ustc.edu.cn",
            wait_until="networkidle",
            timeout=self.timeout_ms,
        )

        self.logger.info("login state machine start")
        success = await self._run_login_state_machine()
        if not success:
            self.logger.error("login state machine failed")
            raise RuntimeError("Login failed: state machine did not reach success URL")

        self.logger.info("login state machine success")

    def _success_reached(self) -> bool:
        return bool(self.page and ("cas-success" in self.page.url))

    async def _has_credentials_form(self) -> bool:
        try:
            user = self.page.locator('input[name="username"]:not([type="hidden"])')
            pwd = self.page.locator('input[type="password"]:not([type="hidden"])')
            submit = self.page.locator("button#submitBtn")

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
        self.logger.info("action fill_credentials")
        await self.page.fill(
            'input[name="username"][autocomplete="username"]:not([type="hidden"])',
            strict=True,
            value=self.username,
        )
        await self.page.fill(
            'input[type="password"]:not([type="hidden"])',
            strict=True,
            value=self.password,
        )
        await self.page.click("button#submitBtn", strict=True)

    async def _has_totp_tab(self) -> bool:
        try:
            tab = self.page.locator("div.ant-tabs-tab:nth-of-type(2)")
            ok = await tab.count() > 0
            self.logger.debug(f"detector totp_tab ok={ok}")
            return ok
        except Exception as e:
            self.logger.debug(f"detector totp_tab error={e}")
            return False

    async def _fill_totp(self) -> None:
        if not self.totp:
            raise ValueError("totp_url is not set")

        self.logger.info("action fill_totp")
        await self.page.click("div.ant-tabs-tab:nth-of-type(2)", strict=True)
        await self.page.fill("input.opt_code_input", strict=True, value=self.totp.now())
        await self.page.click('button[type="submit"]', strict=True)

    async def _run_login_state_machine(self, max_turns: int = 10) -> bool:
        class LoginState(Enum):
            FILL_CREDENTIALS = "FILL_CREDENTIALS"
            FILL_TOTP = "FILL_TOTP"
            DONE = "DONE"
            UNKNOWN = "UNKNOWN"

        async def detect_login_state() -> LoginState:
            if self._success_reached():
                return LoginState.DONE
            elif await self._has_credentials_form():
                return LoginState.FILL_CREDENTIALS
            elif await self._has_totp_tab():
                return LoginState.FILL_TOTP
            else:
                return LoginState.UNKNOWN

        for i in range(1, max_turns + 1):
            await self.page.wait_for_timeout(5 * 1000)

            state = await detect_login_state()
            self.logger.info(f"login turn={i} state={state.value}")

            try:
                if state == LoginState.DONE:
                    self.logger.info("login success")
                    return True
                elif state == LoginState.FILL_CREDENTIALS:
                    await self._fill_credentials()
                    continue
                elif state == LoginState.FILL_TOTP:
                    await self._fill_totp()
                    continue
                elif state == LoginState.UNKNOWN:
                    self.logger.error("login unknown state")
                    await self.page.wait_for_timeout(5 * 1000)
                    continue
            except Exception as e:
                self.logger.error(f"login error={e}")
                return False

        self.logger.info(f"login failed")
        return False

    async def _after_login(self):
        """Navigate to services after login"""

        await self.page.goto(
            "https://passport.ustc.edu.cn/login?service=https://catalog.ustc.edu.cn/ustc_cas_login?next=https://catalog.ustc.edu.cn/",
            wait_until="networkidle",
            timeout=self.timeout_ms,
        )

        await self.page.goto(
            "https://passport.ustc.edu.cn/login?service=https%3A%2F%2Fjw.ustc.edu.cn%2Fucas-sso%2Flogin",
            wait_until="networkidle",
            timeout=self.timeout_ms,
        )
