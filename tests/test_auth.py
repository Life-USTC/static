import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.http import RequestSession
from src.platforms.passport import (
    USTCSession,
    _create_request_http_client,
)


class RequestSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_session_smoke(self):
        attempts = {}

        async def handler(request):
            path = request.url.path
            attempts[path] = attempts.get(path, 0) + 1
            if path == "/retry" and attempts[path] < 3:
                return httpx.Response(503)
            if path == "/forbidden":
                return httpx.Response(403)
            if path == "/for-std/course-select":
                return httpx.Response(
                    302,
                    headers={
                        "location": path + "/" + request.headers["cookie"].split("=")[1]
                    },
                )
            if request.method == "POST":
                self.assertEqual(json.loads(request.content), {"lessonIds": ["1"]})
                self.assertIn("application/json", request.headers["content-type"])
            self.assertIn("SESSION=", request.headers["cookie"])
            return httpx.Response(200, json={"ok": True})

        from src.platforms.jw import _get_jw_user_id

        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                cookies={"SESSION": "101"},
                follow_redirects=True,
            ) as first,
            httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                cookies={"SESSION": "202"},
                follow_redirects=True,
            ) as second,
        ):
            session = RequestSession(client=first, page=None)
            with patch("src.http.retry_delay", return_value=0):
                self.assertEqual(
                    await session.get_json("https://example.test/retry"), {"ok": True}
                )
                await session.post_json(
                    "https://example.test/post", data={"lessonIds": ["1"]}
                )
                with self.assertRaises(httpx.HTTPStatusError):
                    await session.get_json("https://example.test/forbidden")
            self.assertEqual(await _get_jw_user_id(session), "101")
            self.assertEqual(
                await _get_jw_user_id(RequestSession(client=second, page=None)), "202"
            )
            self.assertEqual(await _get_jw_user_id(session), "101")
        self.assertEqual(attempts["/retry"], 3)
        self.assertEqual(attempts["/forbidden"], 1)
        self.assertEqual(attempts["/for-std/course-select"], 2)

    async def test_authenticated_client_ignores_proxy(self):
        with patch.dict(os.environ, {"ALL_PROXY": "socks5://127.0.0.1:1"}):
            async with _create_request_http_client(
                cookies=httpx.Cookies(), user_agent="test"
            ) as client:
                self.assertIsInstance(client, httpx.AsyncClient)


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
                "src.platforms.passport.async_playwright",
                return_value=_FakeManager(playwright),
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
                "src.platforms.passport.async_playwright",
                return_value=_FakeManager(playwright),
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

    async def __aexit__(self, *_):
        await self.playwright.stop()


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
        self.page = type(
            "Page",
            (),
            {"close": AsyncMock(), "set_default_timeout": lambda self, _: None},
        )()
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
