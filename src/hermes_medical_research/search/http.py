from __future__ import annotations

import asyncio
import email.utils
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import httpx

from hermes_medical_research import __version__


class SourceError(RuntimeError):
    """A provider returned an unusable response."""


class AsyncRateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            delay = self.interval - (loop.time() - self._last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = loop.time()


class HttpSession:
    def __init__(
        self,
        *,
        intervals: dict[str, float] | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        defaults = {
            "ncbi": 0.34,
            "openalex": 0.1,
            "semantic-scholar": 1.0,
            "scopus": 0.2,
        }
        defaults.update(intervals or {})
        self.limiters = defaultdict(lambda: AsyncRateLimiter(0.2))
        self.limiters.update(
            {name: AsyncRateLimiter(interval) for name, interval in defaults.items()}
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": f"hermes-medical-research/{__version__}"},
        )

    async def __aenter__(self) -> HttpSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        provider: str,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        attempts: int = 4,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(attempts):
            await self.limiters[provider].wait()
            try:
                response = await self.client.request(method, url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == attempts - 1:
                    break
                await asyncio.sleep(min(2**attempt, 8))
                continue
            if response.status_code not in {429, 500, 502, 503, 504}:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    detail = response.text[:500].strip()
                    raise SourceError(
                        f"{provider} returned HTTP {response.status_code}: {detail}"
                    ) from exc
                return response
            if attempt == attempts - 1:
                detail = response.text[:500].strip()
                raise SourceError(
                    f"{provider} failed after {attempts} attempts "
                    f"(HTTP {response.status_code}): {detail}"
                )
            await asyncio.sleep(_retry_delay(response, attempt))
        raise SourceError(f"{provider} request failed: {last_error}")

    async def json(self, provider: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.request(provider, "GET", url, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"{provider} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise SourceError(f"{provider} returned an unexpected JSON value")
        return data

    async def text(self, provider: str, url: str, **kwargs: Any) -> str:
        response = await self.request(provider, "GET", url, **kwargs)
        return response.text


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After", "").strip()
    if raw:
        try:
            return max(0.0, min(float(raw), 30.0))
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, min((retry_at - datetime.now(UTC)).total_seconds(), 30.0))
            except (TypeError, ValueError):
                pass
    return float(min(2**attempt, 8))
