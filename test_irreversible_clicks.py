"""A click that sends, pays or deletes goes to the founder first.

browser_click_element is mutating=False, which is right for the
ninety-nine clicks in a run that sort a table or open a menu -- and wrong
for the one that sends a message, places an order or deletes something.
Those went straight through, because the approval queue keys on the TOOL
and this tool is usually harmless.

So the check is on the BUTTON. It has to be, given what this layer is
for: an outreach flow ends in "Send", an ad flow ends in "Publish", and
both spend something the founder cannot get back.
"""
from __future__ import annotations

import pytest

from backend.app.browser.primitives import _looks_irreversible as irreversible


@pytest.mark.parametrize("label", [
    "Send", "Send message", "Send message to Priya", "Send invite",
    "Post", "Publish", "Reply", "Submit",
    "Buy now", "Pay", "Place order", "Checkout", "Confirm order",
    "Delete", "Remove", "Deactivate", "Close account",
    "Go live", "Launch campaign", "Publish campaign",
])
def test_a_final_action_is_caught(label):
    assert irreversible(label), label


@pytest.mark.parametrize("label", [
    # Substring matching turned every one of these into a payment
    # confirmation the first time round.
    "Sender name", "Posted 3 days ago", "Post code", "Deleted items",
    "Sort by date", "Chg %", "Performance", "Search", "Next page",
    "Compose", "Save draft", "Preview", "Back to cart", "Sign in",
    "Perf % 1W", "Filter", "Add new filter",
])
def test_ordinary_controls_are_not(label):
    assert not irreversible(label), label


def test_an_empty_label_is_not_irreversible():
    assert not irreversible("")
    assert not irreversible(None)


def test_the_approved_call_is_allowed_through():
    """The approval path re-invokes the same handler with the flag set.
    Without this an approved action would queue itself forever."""
    from backend.app.browser.primitives import _approved
    assert _approved({"_approved": True})
    assert not _approved({})


def test_the_click_handler_checks_before_acting():
    import inspect
    from backend.app.browser import primitives
    src = inspect.getsource(primitives._click_impl)
    before, _, after = src.partition("_click_locator")
    assert "_looks_irreversible" in before, (
        "the check must run BEFORE the click, not after it")
    assert "_queue_for_approval" in before


def test_an_unreachable_queue_does_not_break_the_run(monkeypatch):
    """Breaking every run over an unreachable queue is worse than the
    risk it manages, and every other guard still applies."""
    from backend.app.browser import primitives

    class Sess:
        token = "t"
        page = type("P", (), {"url": "https://x.test"})()

    monkeypatch.setattr(
        "backend.app.actions.approval_queue.get_queue",
        lambda: (_ for _ in ()).throw(RuntimeError("queue down")))
    assert primitives._queue_for_approval(Sess(), "e1", "Send") is None


def test_the_receipt_tells_the_model_to_move_on():
    """A model that re-queues the same action every step burns the whole
    budget waiting for a tap that already happened."""
    import inspect
    from backend.app.browser import primitives
    src = inspect.getsource(primitives._queue_for_approval)
    assert "queued for founder approval" in src
    assert "do not" in src.lower() and "queue it again" in src
    assert "has NOT" in src, "it must be explicit that nothing was clicked"
