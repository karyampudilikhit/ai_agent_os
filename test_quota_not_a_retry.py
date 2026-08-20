"""A spent quota is not a busy server.

Watched live on a news run: OpenRouter's free tier allows 50 model
requests per day. When it was spent, every call came back 429 with
`X-RateLimit-Remaining: 0` and a reset at the next UTC midnight -- ten
hours out. The backoff ladder slept 3s, 5s, 8s, 17s per attempt, four
attempts per step, and the log read like a recovery in progress. It was
not: no amount of waiting inside a run can clear a daily allowance.

So the two kinds of 429 are separated. A momentarily busy shared pool is
waited out, exactly as before. A quota is raised at once, naming how
long it will really be, because a founder deserves to be told to top up
rather than watching a spinner for ten hours.
"""
from __future__ import annotations

import time

import httpx
import pytest

from backend.app.models.provider_adapters.openai_adapter import (
    _describe_wait, _quota_exhausted_until,
)

TOMORROW = (time.time() + 10 * 3600) * 1000.0
BODY = {
    "error": {
        "message": "Rate limit exceeded: free-models-per-day.",
        "code": 429,
        "metadata": {
            "headers": {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int(TOMORROW))},
            "limit_source": "openrouter_free_tier_daily",
        },
    }
}


def _resp(status=429, headers=None, json_body=None):
    return httpx.Response(status, headers=headers or {},
                          json=json_body if json_body is not None else {})


def test_a_daily_cap_is_reported_not_retried():
    got = _quota_exhausted_until(_resp(json_body=BODY))
    assert got is not None
    assert got > 3600, "ten hours out, not a backoff"


def test_the_headers_are_read_when_the_body_has_none():
    got = _quota_exhausted_until(_resp(headers={
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": str(int(TOMORROW)),
    }))
    assert got is not None and got > 3600


def test_a_busy_upstream_is_still_waited_out():
    """The reason the ladder exists. A shared free pool returns 429 all
    the time and backing off genuinely fixes it."""
    soon = (time.time() + 20) * 1000.0
    assert _quota_exhausted_until(_resp(headers={
        "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(soon)),
    })) is None


def test_requests_still_remaining_is_never_a_quota():
    assert _quota_exhausted_until(_resp(headers={
        "x-ratelimit-remaining": "37",
        "x-ratelimit-reset": str(int(TOMORROW)),
    })) is None


def test_a_non_429_is_not_a_quota():
    assert _quota_exhausted_until(_resp(status=503)) is None


def test_a_stale_reset_is_not_a_quota():
    past = (time.time() - 500) * 1000.0
    assert _quota_exhausted_until(_resp(headers={
        "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(past)),
    })) is None


def test_the_error_says_topping_up_is_the_fix_not_waiting():
    from backend.app.models.provider_adapters.openai_adapter import (
        OpenAIAdapterError, OpenAICompatAdapter,
    )
    a = OpenAICompatAdapter(base_url="https://x.test/v1", model="v/m:free",
                            api_key="k")

    class Client:
        def post(self, *a_, **k_):
            return _resp(json_body=BODY)

    a.client = Client()
    with pytest.raises(OpenAIAdapterError) as exc:
        a._post_with_backoff({})
    msg = str(exc.value)
    assert "quota is spent" in msg
    assert "retrying cannot help" in msg
    assert "hour" in msg, "say how long, not just that it failed"


def test_the_wait_is_described_in_units_a_person_reads():
    assert "hour" in _describe_wait(36000)
    assert "minute" in _describe_wait(300)
