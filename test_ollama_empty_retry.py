"""The adapter retries an empty HTTP 200 with a BIGGER token budget.

The live failure, 2026-08-15. A Quant Analyst's agentic loop finished
cleanly -- five steps, run_python among them, real numbers in hand -- and
then the stage that writes the deliverable died on:

    Single-call stage failed: Ollama returned an empty response
    (done_reason='stop', eval_count=1279, thinking=1834 chars)

The specialist was reported to the founder as "produced no output at
all". It had done the work; it could not say so.

The key property under test is that the retry is not the same request
again. Reasoning ate `num_predict` before `response` got any, so an
identical retry reproduces the outcome -- only a bigger budget can
change it.

Offline: a fake httpx client, no daemon needed.
"""
from __future__ import annotations

import pytest

from backend.app.models.provider_adapters import ollama_adapter as mod
from backend.app.models.provider_adapters.ollama_adapter import (
    OllamaAdapter,
    OllamaAdapterError,
)


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """Records every request's num_predict and replays scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.budgets = []

    def post(self, url, json=None, timeout=None):
        self.budgets.append(json["options"]["num_predict"])
        return self.responses.pop(0) if self.responses else _Resp(
            payload={"response": "fallback"})

    def get(self, url, timeout=None):
        return _Resp(payload={"models": []})

    def close(self):
        pass


def _adapter(responses, monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    # Patch the client class BEFORE constructing: the constructor runs a
    # liveness probe, and letting that hit a real socket cost ~3s per
    # adapter (20s across this file) waiting on a hostname that does not
    # resolve.
    client = _Client(responses)
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: client)
    a = OllamaAdapter(base_url="http://x", model="test")
    return a


EMPTY_STOP = _Resp(payload={
    "response": "", "thinking": "x" * 1834,
    "done_reason": "stop", "eval_count": 1279,
})


def _empty_length():
    return _Resp(payload={
        "response": "", "thinking": "y" * 900,
        "done_reason": "length", "eval_count": 400,
    })


def test_empty_response_is_retried_with_a_larger_budget(monkeypatch):
    a = _adapter([EMPTY_STOP, _Resp(payload={"response": "the deliverable"})],
                 monkeypatch)

    assert a.chat_completion("write it", max_tokens=2000) == "the deliverable"

    budgets = a.client.budgets
    assert len(budgets) == 2, "expected exactly one retry, saw %d calls" % len(budgets)
    assert budgets[0] == 2000
    assert budgets[1] > budgets[0], (
        "retried with the SAME budget (%d) -- that reproduces the failure "
        "rather than fixing it" % budgets[1])


def test_it_fires_on_done_reason_stop_not_only_length(monkeypatch):
    """The live failure reported done_reason='stop'. Keying the retry on
    'length' alone would have missed the exact case this exists for."""
    a = _adapter([EMPTY_STOP, _Resp(payload={"response": "ok"})], monkeypatch)

    assert a.chat_completion("go", max_tokens=1000) == "ok"
    assert len(a.client.budgets) == 2


def test_budget_grows_each_time_and_stops_at_the_ceiling(monkeypatch):
    a = _adapter([_empty_length() for _ in range(6)], monkeypatch)

    with pytest.raises(OllamaAdapterError) as exc:
        a.chat_completion("go", max_tokens=300)

    budgets = a.client.budgets
    assert budgets == [300, 900, 2700], budgets
    assert len(budgets) == mod.OLLAMA_EMPTY_RETRIES + 1
    assert all(b <= mod.OLLAMA_EMPTY_TOKEN_CEILING for b in budgets)

    # The error has to name what was actually tried, or the next person
    # debugging this cannot tell a small budget from a broken model.
    msg = str(exc.value)
    assert "2700" in msg and "300" in msg


def test_still_raises_rather_than_returning_empty_content(monkeypatch):
    """The original rule holds: an empty string must never travel
    downstream as if it were the model's answer."""
    a = _adapter([_empty_length() for _ in range(6)], monkeypatch)

    with pytest.raises(OllamaAdapterError):
        a.chat_completion("go", max_tokens=300)


def test_a_request_already_at_the_ceiling_is_not_retried(monkeypatch):
    """No point growing a budget that cannot grow -- that would just be
    the same request twice."""
    a = _adapter([_empty_length() for _ in range(4)], monkeypatch)

    with pytest.raises(OllamaAdapterError):
        a.chat_completion("go", max_tokens=mod.OLLAMA_EMPTY_TOKEN_CEILING)

    assert len(a.client.budgets) == 1, a.client.budgets


def test_a_good_response_is_never_retried(monkeypatch):
    a = _adapter([_Resp(payload={"response": "first time"})], monkeypatch)

    assert a.chat_completion("go", max_tokens=500) == "first time"
    assert a.client.budgets == [500]


def test_5xx_and_empty_retries_compose(monkeypatch):
    """A 500 burst followed by an empty 200 must recover from both. The
    500 retry keeps the same budget; only the empty one grows it."""
    a = _adapter([
        _Resp(status_code=500, text="boom"),
        EMPTY_STOP,
        _Resp(payload={"response": "recovered"}),
    ], monkeypatch)

    assert a.chat_completion("go", max_tokens=1000) == "recovered"

    budgets = a.client.budgets
    assert budgets[0] == 1000 and budgets[1] == 1000, (
        "the 5xx retry must not change the token budget: %s" % budgets)
    assert budgets[2] > 1000, "the empty retry must grow it: %s" % budgets
