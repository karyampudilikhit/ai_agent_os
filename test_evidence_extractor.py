"""Smoke test: EvidenceExtractor (evidence receipts feature).

Covers the deterministic parts — parsing, validation, the "verified
without a URL gets downgraded" safety check, and quiet failure modes.
The live-model path (does a real deliverable actually get useful
claims extracted) is exercised interactively via the Playground.

Run:
    py -3 test_evidence_extractor.py
"""
from __future__ import annotations

from backend.app.critique.evidence_extractor import EvidenceExtractor


class _FakeAdapter:
    def __init__(self, response: str = "", raise_on_call: bool = False):
        self.response = response
        self.raise_on_call = raise_on_call
        self.last_prompt = None

    def chat_completion(self, prompt, **kwargs):
        self.last_prompt = prompt
        if self.raise_on_call:
            raise RuntimeError("adapter offline")
        return self.response


def test_empty_deliverable_returns_empty_list() -> None:
    ext = EvidenceExtractor(model_adapter=_FakeAdapter())
    assert ext.extract("") == []
    assert ext.extract("   ") == []
    print("[ok] empty deliverable -> empty list, no call made")


def test_llm_failure_returns_empty_list() -> None:
    ext = EvidenceExtractor(model_adapter=_FakeAdapter(raise_on_call=True))
    assert ext.extract("Some deliverable with a $999/mo claim.") == []
    print("[ok] LLM failure -> empty list (evidence is enhancement, not a hard dependency)")


def test_parses_all_three_statuses() -> None:
    response = """{"claims": [
        {"text": "Stripe fee is 2.9% + 30c", "status": "verified", "source": "https://stripe.com/pricing"},
        {"text": "TAM for this niche", "status": "flagged_unknown", "source": ""},
        {"text": "Vanta charges $999/month", "status": "unsourced_claim", "source": ""}
    ]}"""
    ext = EvidenceExtractor(model_adapter=_FakeAdapter(response=response))
    claims = ext.extract("some deliverable text")
    assert len(claims) == 3
    statuses = {c["status"] for c in claims}
    assert statuses == {"verified", "flagged_unknown", "unsourced_claim"}
    print("[ok] all three statuses parse correctly")


def test_verified_without_url_gets_downgraded() -> None:
    """A claim the model LABELS 'verified' but with no real URL is a
    contradiction — this is exactly the dangerous pattern from testing
    (confident claim, no real source). Must not be trusted blindly."""
    response = """{"claims": [
        {"text": "Vanta charges $999/month", "status": "verified", "source": ""},
        {"text": "Vanta charges $999/month", "status": "verified", "source": "not-a-url"}
    ]}"""
    ext = EvidenceExtractor(model_adapter=_FakeAdapter(response=response))
    claims = ext.extract("deliverable")
    assert len(claims) == 2
    for c in claims:
        assert c["status"] == "unsourced_claim", c
        assert c["source"] == ""
    print("[ok] 'verified' without a real URL is downgraded to unsourced_claim")


def test_invalid_status_dropped() -> None:
    response = """{"claims": [
        {"text": "Real claim", "status": "verified", "source": "https://real.com"},
        {"text": "Bad claim", "status": "made_up_status", "source": ""}
    ]}"""
    ext = EvidenceExtractor(model_adapter=_FakeAdapter(response=response))
    claims = ext.extract("deliverable")
    assert len(claims) == 1
    assert claims[0]["text"] == "Real claim"
    print("[ok] entries with invalid status are dropped, not crashed on")


def test_fenced_json_parses() -> None:
    response = """```json
{"claims": [{"text": "x", "status": "flagged_unknown", "source": ""}]}
```"""
    ext = EvidenceExtractor(model_adapter=_FakeAdapter(response=response))
    claims = ext.extract("deliverable")
    assert len(claims) == 1
    print("[ok] fenced ```json response parses cleanly")


def test_unparseable_response_returns_empty() -> None:
    ext = EvidenceExtractor(model_adapter=_FakeAdapter(response="not json at all"))
    assert ext.extract("deliverable") == []
    print("[ok] unparseable response -> empty list, no crash")


def test_summarize_counts() -> None:
    ext = EvidenceExtractor(model_adapter=_FakeAdapter())
    claims = [
        {"text": "a", "status": "verified", "source": "https://x.com"},
        {"text": "b", "status": "verified", "source": "https://y.com"},
        {"text": "c", "status": "flagged_unknown", "source": ""},
        {"text": "d", "status": "unsourced_claim", "source": ""},
    ]
    counts = ext.summarize(claims)
    assert counts == {"verified": 2, "flagged_unknown": 1, "unsourced_claim": 1}
    print("[ok] summarize() produces correct counts for a UI badge")


if __name__ == "__main__":
    test_empty_deliverable_returns_empty_list()
    test_llm_failure_returns_empty_list()
    test_parses_all_three_statuses()
    test_verified_without_url_gets_downgraded()
    test_invalid_status_dropped()
    test_fenced_json_parses()
    test_unparseable_response_returns_empty()
    test_summarize_counts()
    print("\nAll EvidenceExtractor checks passed.")
