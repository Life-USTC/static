import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.utils.auth import (
    LoginConfig,
    RequestSession,
    USTCSession,
    _create_request_http_client,
)


class RequestSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_http_client_ignores_environment_proxy(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ALL_PROXY": "socks5://127.0.0.1:1",
                "HTTPS_PROXY": "socks5://127.0.0.1:1",
                "HTTP_PROXY": "socks5://127.0.0.1:1",
            },
        ):
            client = _create_request_http_client(
                cookies=httpx.Cookies(),
                user_agent="test-agent",
            )

        try:
            self.assertIsInstance(client, httpx.AsyncClient)
        finally:
            await client.aclose()

    async def test_get_json_uses_http_client_cookies(self) -> None:
        seen_cookie = ""

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_cookie
            seen_cookie = request.headers.get("cookie", "")
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            cookies={"SESSION": "abc"},
        )
        try:
            session = RequestSession(client=client, page=None)

            payload = await session.get_json(
                "https://catalog.ustc.edu.cn/api/teach/semester/list"
            )

            self.assertEqual(payload, {"ok": True})
            self.assertIn("SESSION=abc", seen_cookie)
        finally:
            await client.aclose()

    async def test_post_json_sends_json_body(self) -> None:
        seen_content_type = ""
        seen_payload = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_content_type, seen_payload
            seen_content_type = request.headers.get("content-type", "")
            seen_payload = json.loads(request.content.decode())
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            session = RequestSession(client=client, page=None)

            payload = await session.post_json(
                "https://jw.ustc.edu.cn/ws/schedule-table/datum",
                data={"lessonIds": ["1"]},
            )

            self.assertEqual(payload, {"ok": True})
            self.assertEqual(seen_content_type, "application/json")
            self.assertEqual(seen_payload, {"lessonIds": ["1"]})
        finally:
            await client.aclose()

    async def test_post_json_honors_per_request_transient_retries(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            session = RequestSession(client=client, page=None)
            with (
                patch.object(session, "_request_retry_wait_ms", return_value=0),
                self.assertRaises(httpx.HTTPStatusError),
            ):
                await session.post_json(
                    "https://jw.ustc.edu.cn/ws/schedule-table/datum",
                    data={"lessonIds": ["1"]},
                    transient_retries=2,
                )
        finally:
            await client.aclose()

        self.assertEqual(attempts, 3)


class USTCSessionConfigTest(unittest.TestCase):
    def test_login_timeout_defaults_to_one_minute(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = LoginConfig.from_env()

        self.assertEqual(config.timeout_ms, 60_000)

    def test_after_login_services_default_to_enabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "USTC_PASSPORT_USERNAME": "user",
                "USTC_PASSPORT_PASSWORD": "password",
                "USTC_PASSPORT_TOTP_URL": "",
            },
        ):
            session = USTCSession()

        self.assertTrue(session.after_login_services)

    def test_after_login_services_can_be_disabled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "USTC_PASSPORT_USERNAME": "user",
                "USTC_PASSPORT_PASSWORD": "password",
                "USTC_PASSPORT_TOTP_URL": "",
            },
        ):
            session = USTCSession(after_login_services=False)

        self.assertFalse(session.after_login_services)


class USTCSessionCleanupTest(unittest.IsolatedAsyncioTestCase):
    def create_session(self) -> USTCSession:
        with patch.dict(
            os.environ,
            {
                "USTC_PASSPORT_USERNAME": "user",
                "USTC_PASSPORT_PASSWORD": "password",
                "USTC_PASSPORT_TOTP_URL": "",
            },
        ):
            return USTCSession(after_login_services=False)

    async def test_launch_failure_stops_playwright(self) -> None:
        playwright = _FakePlaywright(launch_error=RuntimeError("launch failed"))
        session = self.create_session()

        with (
            patch(
                "src.utils.auth.async_playwright", return_value=_FakeManager(playwright)
            ),
            self.assertRaisesRegex(RuntimeError, "launch failed"),
        ):
            await session.__aenter__()

        playwright.stop.assert_awaited_once()

    async def test_login_failure_closes_all_browser_resources(self) -> None:
        playwright = _FakePlaywright()
        session = self.create_session()

        with (
            patch(
                "src.utils.auth.async_playwright", return_value=_FakeManager(playwright)
            ),
            patch.object(
                session, "_login", AsyncMock(side_effect=RuntimeError("login failed"))
            ),
            self.assertRaisesRegex(RuntimeError, "login failed"),
        ):
            await session.__aenter__()

        playwright.page.close.assert_awaited_once()
        playwright.context.close.assert_awaited_once()
        playwright.browser.close.assert_awaited_once()
        playwright.stop.assert_awaited_once()


class _FakeManager:
    def __init__(self, playwright) -> None:
        self.playwright = playwright

    async def __aenter__(self):
        return self.playwright


class _FakeChromium:
    def __init__(self, browser, launch_error=None) -> None:
        self.browser = browser
        self.launch_error = launch_error

    async def launch(self, *, headless):
        if self.launch_error:
            raise self.launch_error
        return self.browser


class _FakePlaywright:
    def __init__(self, launch_error=None) -> None:
        self.page = type("Page", (), {"close": AsyncMock()})()
        self.context = type(
            "Context",
            (),
            {"new_page": AsyncMock(return_value=self.page), "close": AsyncMock()},
        )()
        self.browser = type(
            "Browser",
            (),
            {"new_context": AsyncMock(return_value=self.context), "close": AsyncMock()},
        )()
        self.chromium = _FakeChromium(self.browser, launch_error)
        self.stop = AsyncMock()


if __name__ == "__main__":
    unittest.main()
