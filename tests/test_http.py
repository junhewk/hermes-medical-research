from __future__ import annotations

import httpx
import pytest

from hermes_medical_search.http import HttpSession, SourceError


@pytest.mark.asyncio
async def test_http_retries_rate_limit_then_succeeds() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="wait")
        return httpx.Response(200, json={"ok": True})

    async with HttpSession(
        intervals={"test": 0}, transport=httpx.MockTransport(handler)
    ) as session:
        assert await session.json("test", "https://example.test") == {"ok": True}
    assert calls == 3


@pytest.mark.asyncio
async def test_http_serializes_terminal_provider_error() -> None:
    async with HttpSession(
        intervals={"test": 0},
        transport=httpx.MockTransport(lambda _request: httpx.Response(403, text="denied")),
    ) as session:
        with pytest.raises(SourceError, match="HTTP 403"):
            await session.json("test", "https://example.test")
