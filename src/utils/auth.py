import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from pyotp import TOTP, parse_uri


class LoginState(Enum):
    FILL_CREDENTIALS = "FILL_CREDENTIALS"
    FILL_TOTP = "FILL_TOTP"
    DONE = "DONE"
    UNKNOWN = "UNKNOWN"


CATALOG_SSO_URL = (
    "https://passport.ustc.edu.cn/login?"
    "service=https://catalog.ustc.edu.cn/ustc_cas_login?next=https://catalog.ustc.edu.cn/"
)
JW_SSO_URL = "https://jw.ustc.edu.cn/ucas-sso/login"
JW_COURSE_SELECT_URL = "https://jw.ustc.edu.cn/for-std/course-select"


@dataclass(frozen=True)
class LoginConfig:
    timeout_ms: int
    attempts: int
    state_max_turns: int
    turn_wait_ms: int
    state_redetect_wait_ms: int
    state_redetect_attempts: int

    @classmethod
    def from_env(cls) -> "LoginConfig":
        timeout_ms = int(os.getenv("USTC_TIMEOUT_MS", "0"))
        attempts = max(int(os.getenv("USTC_LOGIN_ATTEMPTS", "3")), 1)
        state_max_turns = max(int(os.getenv("USTC_LOGIN_STATE_MAX_TURNS", "10")), 1)
        turn_wait_ms = max(int(os.getenv("USTC_LOGIN_TURN_WAIT_MS", "5000")), 0)
        state_redetect_wait_ms = max(
            int(os.getenv("USTC_LOGIN_STATE_REDETECT_WAIT_MS", "1500")), 0
        )
        state_redetect_attempts = max(
            int(os.getenv("USTC_LOGIN_STATE_REDETECT_ATTEMPTS", "3")), 1
        )
        return cls(
            timeout_ms=timeout_ms,
            attempts=attempts,
            state_max_turns=state_max_turns,
            turn_wait_ms=turn_wait_ms,
            state_redetect_wait_ms=state_redetect_wait_ms,
            state_redetect_attempts=state_redetect_attempts,
        )


class LoginStep(Protocol):
    state: LoginState

    async def detect(self, session: "USTCSession") -> bool: ...

    async def act(self, session: "USTCSession") -> None: ...


@dataclass(frozen=True)
class LoginStepConfig:
    state: LoginState
    detect_fn: Callable[["USTCSession"], Awaitable[bool]]
    act_fn: Callable[["USTCSession"], Awaitable[None]]


@dataclass(frozen=True)
class ConfiguredLoginStep:
    config: LoginStepConfig

    @property
    def state(self) -> LoginState:
        return self.config.state

    async def detect(self, session: "USTCSession") -> bool:
        return await self.config.detect_fn(session)

    async def act(self, session: "USTCSession") -> None:
        await self.config.act_fn(session)


async def _detect_credentials_step(session: "USTCSession") -> bool:
    try:
        user = session.page.locator('input[name="username"]:not([type="hidden"])')
        pwd = session.page.locator('input[type="password"]:not([type="hidden"])')
        submit = session.page.locator("button#submitBtn")
        ok = (
            await user.count() > 0
            and await pwd.count() > 0
            and await submit.count() > 0
        )
        session.logger.debug(f"detector credentials_form ok={ok}")
        return ok
    except Exception as e:
        session.logger.debug(f"detector credentials_form error={e}")
        return False


async def _act_credentials_step(session: "USTCSession") -> None:
    session.logger.info("action fill_credentials")
    await session.page.fill(
        'input[name="username"][autocomplete="username"]:not([type="hidden"])',
        strict=True,
        value=session.username,
    )
    await session.page.fill(
        'input[type="password"]:not([type="hidden"])',
        strict=True,
        value=session.password,
    )
    await session.page.click("button#submitBtn", strict=True)


async def _detect_totp_step(session: "USTCSession") -> bool:
    try:
        tab = session.page.locator("div.ant-tabs-tab:nth-of-type(2)")
        ok = await tab.count() > 0
        session.logger.debug(f"detector totp_tab ok={ok}")
        return ok
    except Exception as e:
        session.logger.debug(f"detector totp_tab error={e}")
        return False


async def _act_totp_step(session: "USTCSession") -> None:
    if not session.totp:
        raise ValueError("totp_url is not set")

    session.logger.info("action fill_totp")
    await session.page.click("div.ant-tabs-tab:nth-of-type(2)", strict=True)
    await session.page.fill(
        "input.opt_code_input", strict=True, value=session.totp.now()
    )
    await session.page.click('button[type="submit"]', strict=True)


class RequestSession:
    class _Response:
        def __init__(self, response: httpx.Response):
            self._response = response
            self.url = str(response.url)
            self.status = response.status_code
            self.headers = response.headers

        async def json(self) -> Any:
            return self._response.json()

        async def text(self) -> str:
            return self._response.text

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        page: Page | None,
        timeout_ms: int = 10 * 60 * 1000,
        fail_on_status_code: bool = True,
        max_retries: int = 100,
        transient_retries: int = 3,
    ):
        self.client = client
        self.page = page
        self.timeout_ms = timeout_ms
        self.fail_on_status_code = fail_on_status_code
        self.max_retries = max_retries
        self.transient_retries = transient_retries
        self.logger = logging.getLogger(__name__)

    def _is_transient_request_error(self, e: Exception) -> bool:
        if isinstance(e, httpx.HTTPStatusError):
            return e.response.status_code in {429, 500, 502, 503, 504}
        message = str(e).lower()
        return any(
            marker in message
            for marker in (
                " 429 ",
                " 500 ",
                " 502 ",
                " 503 ",
                " 504 ",
                "gateway time-out",
                "timeout",
                "timed out",
                "econnreset",
                "socket hang up",
            )
        )

    def _request_retry_wait_ms(self, attempt: int) -> int:
        return min(2_000 * attempt, 10_000)

    def _timeout_seconds(self, timeout_ms: int) -> float | None:
        if timeout_ms <= 0:
            return None
        return timeout_ms / 1000

    async def _request_with_transient_retries(
        self,
        *,
        method: str,
        url: str,
        request: Callable[[], Awaitable[httpx.Response]],
        retries: int,
    ) -> "RequestSession._Response":
        for attempt in range(1, retries + 2):
            self.logger.info(f"{method} {url}")
            try:
                r = await request()
            except httpx.HTTPError as e:
                if attempt > retries or not self._is_transient_request_error(e):
                    raise
                wait_ms = self._request_retry_wait_ms(attempt)
                self.logger.warning(
                    "%s %s transient failure on attempt %s/%s, retry in %sms: %s",
                    method,
                    url,
                    attempt,
                    retries + 1,
                    wait_ms,
                    e,
                )
                await asyncio.sleep(wait_ms / 1000)
                continue

            self.logger.info(f"{method} {url} done")
            return self._Response(r)

        raise RuntimeError(f"{method} {url} retry loop exhausted")

    async def get(self, url: str, **kwargs: Any):
        timeout_ms = kwargs.get("timeout", self.timeout_ms)
        fail_on_status_code = kwargs.get(
            "fail_on_status_code", self.fail_on_status_code
        )
        params: dict[str, Any] = {
            "url": url,
            "timeout": self._timeout_seconds(timeout_ms),
        }
        if "headers" in kwargs and kwargs["headers"] is not None:
            params["headers"] = kwargs["headers"]

        async def request() -> httpx.Response:
            response = await self.client.get(**params)
            if fail_on_status_code:
                response.raise_for_status()
            return response

        return await self._request_with_transient_retries(
            method="GET",
            url=url,
            request=request,
            retries=kwargs.get("transient_retries", self.transient_retries),
        )

    async def post(self, url: str, data: Any = None, **kwargs: Any):
        timeout_ms = kwargs.get("timeout", self.timeout_ms)
        fail_on_status_code = kwargs.get(
            "fail_on_status_code", self.fail_on_status_code
        )
        params: dict[str, Any] = {
            "url": url,
            "timeout": self._timeout_seconds(timeout_ms),
        }
        if "headers" in kwargs and kwargs["headers"] is not None:
            params["headers"] = kwargs["headers"]

        if kwargs.get("json_body"):
            params["json"] = data
        else:
            params["data"] = data

        async def request() -> httpx.Response:
            response = await self.client.post(**params)
            if fail_on_status_code:
                response.raise_for_status()
            return response

        return await self._request_with_transient_retries(
            method="POST",
            url=url,
            request=request,
            retries=kwargs.get("transient_retries", self.transient_retries),
        )

    async def get_json(self, url: str, **kwargs: Any):
        r = await self.get(url, **kwargs)
        return await r.json()

    async def post_json(self, url: str, data: Any = None, **kwargs: Any):
        r = await self.post(url, data=data, json_body=True, **kwargs)
        return await r.json()

    async def sync_cookies_from_page(self) -> None:
        if not self.page:
            return
        _add_browser_cookies_to_jar(
            self.client.cookies,
            await self.page.context.cookies(),
        )

    async def close(self) -> None:
        await self.client.aclose()


def _add_browser_cookies_to_jar(
    jar: httpx.Cookies, browser_cookies: list[dict[str, Any]]
) -> None:
    for cookie in browser_cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        jar.set(
            name,
            value,
            domain=cookie.get("domain") or "",
            path=cookie.get("path") or "/",
        )


def _create_request_http_client(
    *, cookies: httpx.Cookies, user_agent: str
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        cookies=cookies,
        follow_redirects=True,
        trust_env=False,
        headers={
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "user-agent": user_agent,
        },
    )


class USTCSession:
    """Context manager for USTC authentication and returns a RequestSession"""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright: Playwright
        self.browser: Browser
        self.context: BrowserContext
        self.page: Page
        self.request_session: RequestSession | None = None
        self.totp: TOTP | None = None
        self.logger = logging.getLogger(__name__)

        load_dotenv()
        self.login_config = LoginConfig.from_env()
        self.timeout_ms = self.login_config.timeout_ms
        self.username = os.getenv("USTC_PASSPORT_USERNAME", "")
        self.password = os.getenv("USTC_PASSPORT_PASSWORD", "")
        if self.username == "" or self.password == "":
            raise ValueError(
                "USTC_PASSPORT_USERNAME and USTC_PASSPORT_PASSWORD must be set in "
                "environment variables"
            )
        self.totp_url = os.getenv("USTC_PASSPORT_TOTP_URL", "")

        if self.totp_url:
            self.totp = parse_uri(self.totp_url)  # type: ignore

        self.login_steps: list[LoginStep] = [
            ConfiguredLoginStep(
                LoginStepConfig(
                    state=LoginState.FILL_CREDENTIALS,
                    detect_fn=_detect_credentials_step,
                    act_fn=_act_credentials_step,
                )
            ),
            ConfiguredLoginStep(
                LoginStepConfig(
                    state=LoginState.FILL_TOTP,
                    detect_fn=_detect_totp_step,
                    act_fn=_act_totp_step,
                )
            ),
        ]
        self.login_steps_by_state: dict[LoginState, LoginStep] = {
            step.state: step for step in self.login_steps
        }

    async def __aenter__(self) -> RequestSession:
        """Initialize browser, perform login and return RequestSession"""
        self.playwright = await async_playwright().__aenter__()
        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(locale="zh-CN")
        self.page = await self.context.new_page()

        await self._login()
        await self._after_login()

        client = await self._create_http_client()
        timeout_ms = self.timeout_ms if self.timeout_ms > 0 else 10 * 60 * 1000
        self.request_session = RequestSession(
            client=client,
            page=self.page,
            timeout_ms=timeout_ms,
        )
        return self.request_session

    async def __aexit__(self, exc_type, exc, tb):
        """Cleanup browser resources"""
        with suppress(Exception):
            if self.request_session:
                await self.request_session.close()
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
        for attempt in range(1, self.login_config.attempts + 1):
            self.logger.info("login attempt %s/%s", attempt, self.login_config.attempts)

            if attempt > 1:
                await self.context.clear_cookies()

            if await self._run_login_attempt(attempt):
                self.logger.info("login success on attempt %s", attempt)
                return

            if attempt < self.login_config.attempts:
                wait_ms = self._retry_wait_ms(attempt)
                self.logger.warning(
                    "login attempt %s failed, retry in %sms", attempt, wait_ms
                )
                await self.page.wait_for_timeout(wait_ms)

        self.logger.error("login failed after %s attempts", self.login_config.attempts)
        raise RuntimeError(
            f"Login failed after {self.login_config.attempts} attempts:"
            "state machine did not reach success URL"
        )

    async def _run_login_attempt(self, attempt: int) -> bool:
        try:
            await self.page.goto(
                "https://id.ustc.edu.cn",
                wait_until="networkidle",
                timeout=self.timeout_ms,
            )
            return await self._run_login_state_machine(attempt=attempt)
        except Exception as e:
            self.logger.error("login attempt %s error=%s", attempt, e)
            return False

    def _retry_wait_ms(self, attempt: int) -> int:
        return min(2_000 * attempt, 10_000)

    def _success_reached(self) -> bool:
        return bool(self.page and ("cas-success" in self.page.url))

    def _is_timeout_error(self, e: Exception) -> bool:
        class_name = e.__class__.__name__.lower()
        message = str(e).lower()
        return "timeout" in class_name or "timeout" in message

    async def _detect_login_state(self) -> LoginState:
        if self._success_reached():
            return LoginState.DONE
        for step in self.login_steps:
            if await step.detect(self):
                return step.state
        return LoginState.UNKNOWN

    async def _redetect_login_state(self) -> LoginState:
        for i in range(1, self.login_config.state_redetect_attempts + 1):
            if self.login_config.state_redetect_wait_ms > 0:
                await self.page.wait_for_timeout(
                    self.login_config.state_redetect_wait_ms
                )
            state = await self._detect_login_state()
            self.logger.info("login redetect %s state=%s", i, state.value)
            if state != LoginState.UNKNOWN:
                return state
        return LoginState.UNKNOWN

    async def _run_login_state_machine(
        self, max_turns: int | None = None, attempt: int = 1
    ) -> bool:
        turns = self.login_config.state_max_turns if max_turns is None else max_turns

        for i in range(1, turns + 1):
            if self.login_config.turn_wait_ms > 0:
                await self.page.wait_for_timeout(self.login_config.turn_wait_ms)

            state = await self._detect_login_state()
            self.logger.info(
                "login attempt %s turn %s/%s state=%s",
                attempt,
                i,
                turns,
                state.value,
            )

            try:
                if state == LoginState.DONE:
                    self.logger.info("login success")
                    return True
                if state == LoginState.UNKNOWN:
                    self.logger.error("login unknown state")
                    continue

                step = self.login_steps_by_state.get(state)
                if not step:
                    self.logger.error("login state has no step: %s", state.value)
                    return False
                await step.act(self)
            except Exception as e:
                if self._is_timeout_error(e):
                    self.logger.warning(
                        "login attempt %s timeout at turn %s state=%s error=%s",
                        attempt,
                        i,
                        state.value,
                        e,
                    )
                    redetected_state = await self._redetect_login_state()
                    if redetected_state == LoginState.DONE:
                        self.logger.info("login success after timeout redetect")
                        return True
                    if (
                        redetected_state != LoginState.UNKNOWN
                        and redetected_state != state
                    ):
                        self.logger.info(
                            "login state changed after timeout %s -> %s",
                            state.value,
                            redetected_state.value,
                        )
                        continue
                    self.logger.warning("login timeout fallback to outer retry")
                    return False

                self.logger.error(f"login error={e}")
                return False

        self.logger.info("login failed")
        return False

    async def _after_login(self):
        """Navigate to services after login"""

        await self.page.goto(
            CATALOG_SSO_URL,
            wait_until="networkidle",
            timeout=self.timeout_ms,
        )
        await self._ensure_jw_session()

    async def _create_http_client(self) -> httpx.AsyncClient:
        user_agent = await self.page.evaluate("navigator.userAgent")
        cookies = httpx.Cookies()
        _add_browser_cookies_to_jar(cookies, await self.context.cookies())
        return _create_request_http_client(cookies=cookies, user_agent=user_agent)

    def _jw_user_id_from_url(self, url: str) -> str | None:
        path = urlparse(url).path.rstrip("/")
        user_id = path.split("/")[-1] if path else ""
        return user_id if user_id.isnumeric() else None

    async def _complete_passport_prompt_if_present(self) -> None:
        for _ in range(self.login_config.state_max_turns):
            state = await self._detect_login_state()
            if state not in (LoginState.FILL_CREDENTIALS, LoginState.FILL_TOTP):
                return

            step = self.login_steps_by_state[state]
            await step.act(self)
            if self.login_config.turn_wait_ms > 0:
                await self.page.wait_for_timeout(self.login_config.turn_wait_ms)

        raise RuntimeError("Passport prompt did not clear during JW SSO")

    async def _open_jw_sso(self) -> None:
        login_link = self.page.locator("a", has_text="统一身份认证登录")
        if "jw.ustc.edu.cn/login" in self.page.url and await login_link.count() > 0:
            await login_link.first.click()
            await self.page.wait_for_load_state(
                "networkidle",
                timeout=self.timeout_ms,
            )
        else:
            await self.page.goto(
                JW_SSO_URL,
                wait_until="networkidle",
                timeout=self.timeout_ms,
            )

        await self._complete_passport_prompt_if_present()
        self.logger.info(
            "jw sso returned url=%s title=%s",
            self.page.url,
            await self.page.title(),
        )

    async def _ensure_jw_session(self) -> None:
        final_url = ""
        for attempt in range(1, 4):
            await self.page.goto(
                JW_COURSE_SELECT_URL,
                wait_until="networkidle",
                timeout=self.timeout_ms,
            )
            final_url = self.page.url
            user_id = self._jw_user_id_from_url(final_url)
            if user_id:
                self.logger.info("jw session ready user_id=%s", user_id)
                return

            self.logger.warning(
                "jw session not ready on attempt %s; course-select url=%s",
                attempt,
                final_url,
            )
            await self._open_jw_sso()
            await self.page.wait_for_timeout(min(2_000 * attempt, 5_000))

        raise RuntimeError(
            "Failed to establish JW session; last course-select url=" + final_url
        )
