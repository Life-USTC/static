"""School-specific forms, using Patchright waits and Tenacity for transient retries."""

import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from patchright.async_api import TimeoutError as BrowserTimeoutError
from patchright.async_api import async_playwright
from pyotp import TOTP, parse_uri
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.http import RequestSession, _add_browser_cookies_to_jar

CATALOG_SSO_URL = (
    "https://passport.ustc.edu.cn/login?"
    "service=https://catalog.ustc.edu.cn/ustc_cas_login?next=https://catalog.ustc.edu.cn/"
)
JW_SSO_URL = "https://jw.ustc.edu.cn/ucas-sso/login"
JW_COURSE_SELECT_URL = "https://jw.ustc.edu.cn/for-std/course-select"
JW_USER_URL = re.compile(
    r"^https://jw\.ustc\.edu\.cn/for-std/course-select/(?:[^/?#]+/)*\d+(?:[/?#]|$)"
)


def _create_request_http_client(
    *, cookies: httpx.Cookies, user_agent: str
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        cookies=cookies,
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
        headers={
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "user-agent": user_agent,
        },
    )


class USTCSession:
    def __init__(
        self,
        headless: bool = True,
        *,
        after_login_services: bool = True,
        catalog_login: bool = True,
    ):
        load_dotenv()
        self.headless = headless
        self.after_login_services = after_login_services
        self.catalog_login = catalog_login
        self.timeout_ms = int(os.getenv("USTC_TIMEOUT_MS", "60000"))
        if self.timeout_ms <= 0:
            raise ValueError("USTC_TIMEOUT_MS must be positive")
        self.attempts = max(int(os.getenv("USTC_LOGIN_ATTEMPTS", "3")), 1)
        self.username = os.getenv("USTC_PASSPORT_USERNAME", "")
        self.password = os.getenv("USTC_PASSPORT_PASSWORD", "")
        if not self.username or not self.password:
            raise ValueError(
                "USTC_PASSPORT_USERNAME and USTC_PASSPORT_PASSWORD must be set"
            )
        uri = os.getenv("USTC_PASSPORT_TOTP_URL", "")
        otp = parse_uri(uri) if uri else None
        if otp is not None and not isinstance(otp, TOTP):
            raise ValueError("USTC_PASSPORT_TOTP_URL must describe TOTP")
        self.totp = otp
        self.logger = logging.getLogger(__name__)
        self.resources = AsyncExitStack()

    async def __aenter__(self) -> RequestSession:
        try:
            self.playwright = await self.resources.enter_async_context(
                async_playwright()
            )
            self.browser = await self.playwright.chromium.launch(headless=self.headless)
            self.resources.push_async_callback(self.browser.close)
            self.context = await self.browser.new_context(locale="zh-CN")
            self.resources.push_async_callback(self.context.close)
            self.page = await self.context.new_page()
            self.resources.push_async_callback(self.page.close)
            self.page.set_default_timeout(self.timeout_ms)
            await self._login()
            if self.after_login_services:
                await self._after_login()
            client = await self.resources.enter_async_context(
                await self._create_http_client()
            )
            return RequestSession(
                client=client, page=self.page, timeout_ms=self.timeout_ms
            )
        except BaseException:
            await self.resources.aclose()
            raise

    async def __aexit__(self, *_):
        await self.resources.aclose()

    async def _next_form(self, target: str, previous: str):
        waits = {
            "done": self.page.wait_for_url(
                re.compile(target), wait_until="domcontentloaded"
            )
        }
        if previous != "credentials":
            waits["credentials"] = self.page.locator(
                'input[name="username"]:not([type="hidden"])'
            ).wait_for(state="visible")
        if previous != "totp":
            waits["totp"] = self.page.locator(
                "div.ant-tabs-tab:nth-of-type(2)"
            ).wait_for(state="visible")
        tasks = {stage: asyncio.create_task(wait) for stage, wait in waits.items()}
        try:
            done, _ = await asyncio.wait(
                tasks.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for stage, task in tasks.items():
                if task in done:
                    task.result()
                    return stage
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    async def _finish_login(self, target: str):
        previous = ""
        seen = set()
        try:
            async with asyncio.timeout(self.timeout_ms / 1000):
                while True:
                    stage = await self._next_form(target, previous)
                    if stage == "done":
                        return
                    if stage in seen:
                        raise ValueError(
                            "School login returned to an already submitted form"
                        )
                    seen.add(stage)
                    self.logger.info("School authentication: %s", stage)
                    if stage == "credentials":
                        await self.page.locator(
                            'input[name="username"]:not([type="hidden"])'
                        ).fill(self.username)
                        await self.page.locator(
                            'input[type="password"]:not([type="hidden"])'
                        ).fill(self.password)
                        await self.page.locator("button#submitBtn").click()
                    elif stage == "totp":
                        if self.totp is None:
                            raise ValueError(
                                "School login requires USTC_PASSPORT_TOTP_URL"
                            )
                        await self.page.locator(
                            "div.ant-tabs-tab:nth-of-type(2)"
                        ).click()
                        code = self.page.locator("input.opt_code_input")
                        await code.wait_for(state="visible")
                        await code.fill(self.totp.now())
                        await self.page.locator('button[type="submit"]').click()
                    previous = stage
        except (TimeoutError, BrowserTimeoutError):
            controls = await self.page.locator("input, [role=tab]").evaluate_all(
                "nodes => nodes.filter(n => n.getClientRects().length).map(n => "
                "n.tagName === 'INPUT' ? n.type + ':' + n.name : n.textContent.trim())"
            )
            self.logger.warning(
                "School login stalled on %s; visible controls: %s",
                urlsplit(self.page.url).hostname,
                controls,
            )
            raise

    async def _login(self):
        async def attempt():
            await self.context.clear_cookies()
            await self.page.goto(
                "https://id.ustc.edu.cn",
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            await self._finish_login("cas-success")

        await AsyncRetrying(
            stop=stop_after_attempt(self.attempts),
            retry=retry_if_exception_type((BrowserTimeoutError, TimeoutError)),
            wait=wait_exponential(multiplier=1, max=10),
            before_sleep=lambda state: self.logger.warning(
                "School login timeout; retry %s/%s", state.attempt_number, self.attempts
            ),
            reraise=True,
        )(attempt)

    async def _after_login(self):
        if self.catalog_login:
            await self.page.goto(
                CATALOG_SSO_URL, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            await self._finish_login(
                r"^https://catalog\.ustc\.edu\.cn/(?!ustc_cas_login)"
            )
        await self.page.goto(
            JW_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=self.timeout_ms
        )
        if JW_USER_URL.match(self.page.url):
            return
        if "jw.ustc.edu.cn/login" in self.page.url:
            await self.page.get_by_role("link", name="统一身份认证登录").click()
        else:
            await self.page.goto(
                JW_SSO_URL, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
        await self._finish_login(
            r"^https://jw\.ustc\.edu\.cn/(?!login(?:[/?#]|$)|ucas-sso/login)"
        )
        await self.page.goto(
            JW_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=self.timeout_ms
        )
        await self.page.wait_for_url(
            JW_USER_URL, wait_until="domcontentloaded", timeout=self.timeout_ms
        )

    async def _create_http_client(self) -> httpx.AsyncClient:
        cookies = httpx.Cookies()
        _add_browser_cookies_to_jar(cookies, await self.context.cookies())
        return _create_request_http_client(
            cookies=cookies, user_agent=await self.page.evaluate("navigator.userAgent")
        )
