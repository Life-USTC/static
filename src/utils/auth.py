import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from pyotp import TOTP, parse_uri

from .tools import cache_dir_from_url, save_json


class LoginState(Enum):
    FILL_CREDENTIALS = "FILL_CREDENTIALS"
    FILL_TOTP = "FILL_TOTP"
    DONE = "DONE"
    UNKNOWN = "UNKNOWN"


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
        params: dict[str, Any] = {
            "url": url,
            "timeout": kwargs.get("timeout", self.timeout_ms),
            "fail_on_status_code": kwargs.get(
                "fail_on_status_code", self.fail_on_status_code
            ),
            "max_retries": kwargs.get("max_retries", self.max_retries),
        }
        if "headers" in kwargs and kwargs["headers"] is not None:
            params["headers"] = kwargs["headers"]

        self.logger.info(f"GET {url}")
        r = await self.page.request.get(**params)
        self.logger.info(f"GET {url} done")

        return r

    async def post(self, url: str, data: Any = None, **kwargs: Any):
        params: dict[str, Any] = {
            "url": url,
            "data": data,
            "timeout": kwargs.get("timeout", self.timeout_ms),
            "fail_on_status_code": kwargs.get(
                "fail_on_status_code", self.fail_on_status_code
            ),
            "max_retries": kwargs.get("max_retries", self.max_retries),
        }
        if "headers" in kwargs and kwargs["headers"] is not None:
            params["headers"] = kwargs["headers"]

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

    def __init__(self, headless: bool = True, proxy: dict | None = None):
        self.headless = headless
        self.proxy = proxy
        self.playwright: Playwright
        self.browser: Browser
        self.context: BrowserContext
        self.page: Page
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

        if os.getenv("HTTP_PROXY_URL"):
            self.proxy = {
                "server": os.getenv("HTTP_PROXY_URL", ""),
                "username": os.getenv("HTTP_PROXY_USERNAME", ""),
                "password": os.getenv("HTTP_PROXY_PASSWORD", ""),
                "bypass": "jw.ustc.edu.cn",
            }

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
            "https://passport.ustc.edu.cn/login?service=https://catalog.ustc.edu.cn/ustc_cas_login?next=https://catalog.ustc.edu.cn/",
            wait_until="networkidle",
            timeout=self.timeout_ms,
        )

        await self.page.goto(
            "https://passport.ustc.edu.cn/login?service=https%3A%2F%2Fjw.ustc.edu.cn%2Fucas-sso%2Flogin",
            wait_until="networkidle",
            timeout=self.timeout_ms,
        )
