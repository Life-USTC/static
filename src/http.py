"""HTTP requests shared by the school collectors; browser login stays in auth."""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def transient_error(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRY_STATUSES
    return isinstance(
        error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
    )


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return min(max(float(value), 0.0), 30.0)
    except ValueError:
        pass
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    delay = (date - datetime.now(UTC)).total_seconds()
    return min(max(delay, 0.0), 30.0)


_backoff = wait_exponential(multiplier=1, max=10)


def retry_delay(state: RetryCallState) -> float:
    outcome = state.outcome
    result = (
        outcome.exception()
        if outcome and outcome.failed
        else outcome.result()
        if outcome
        else None
    )
    response = result.response if isinstance(result, httpx.HTTPStatusError) else result
    if isinstance(response, httpx.Response):
        delay = retry_after_seconds(response.headers.get("retry-after"))
        if delay:
            return delay
    return _backoff(state)


def last_result(state: RetryCallState):
    if state.outcome is None:
        raise RuntimeError("Retry finished without an outcome")
    return state.outcome.result()


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
        page: Any | None,
        timeout_ms: int = 10 * 60 * 1000,
        fail_on_status_code: bool = True,
        transient_retries: int = 3,
    ):
        self.client = client
        self.page = page
        self.timeout_ms = timeout_ms
        self.fail_on_status_code = fail_on_status_code
        self.transient_retries = transient_retries
        self.logger = logging.getLogger(__name__)

    async def _request(self, method: str, url: str, *, data: Any = None, **kwargs):
        timeout = kwargs.get("timeout", self.timeout_ms)
        params: dict[str, Any] = {"timeout": timeout / 1000 if timeout > 0 else None}
        if kwargs.get("headers") is not None:
            params["headers"] = kwargs["headers"]
        if method == "POST":
            params["json" if kwargs.get("json_body") else "data"] = data
        retries = kwargs.get("transient_retries", self.transient_retries)

        async def request():
            response = await self.client.request(method, url, **params)
            if kwargs.get("fail_on_status_code", self.fail_on_status_code):
                response.raise_for_status()
            return self._Response(response)

        return await AsyncRetrying(
            stop=stop_after_attempt(retries + 1),
            retry=retry_if_exception(transient_error),
            wait=retry_delay,
            before_sleep=lambda state: self.logger.warning(
                "%s retry %s/%s", method, state.attempt_number, retries
            ),
            reraise=True,
        )(request)

    async def get(self, url: str, **kwargs):
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, data: Any = None, **kwargs):
        return await self._request("POST", url, data=data, **kwargs)

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
    jar: httpx.Cookies, browser_cookies: Sequence[Mapping[str, Any]]
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
