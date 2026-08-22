"""Smoke test: RedditReader.

Runs offline checks always. Runs live OAuth check only when
REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET are set — see reddit_reader.py
docstring for how to get them (reddit.com/prefs/apps, "script" type).

Run with:
    py -3 test_reddit_reader.py
"""
from __future__ import annotations

import os

# Load .env before importing the module so env vars are visible
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from backend.app.tools.reddit_reader import (
    RedditReader,
    extract_subreddits,
    extract_thread_urls,
    should_read_reddit,
)


def test_triggers() -> None:
    assert should_read_reddit("research trends on r/marketing")
    assert should_read_reddit("what are people saying about SaaS pricing")
    assert should_read_reddit(
        "https://www.reddit.com/r/indiehackers/comments/abc123/foo/"
    )
    assert not should_read_reddit("write a poem about the ocean")
    print("[ok] triggers")


def test_extractors() -> None:
    subs = extract_subreddits(
        "compare r/marketing and r/entrepreneur but skip r/marketing"
    )
    assert subs == ["marketing", "entrepreneur"], subs

    threads = extract_thread_urls(
        "look at https://www.reddit.com/r/SaaS/comments/xyz789/pricing/ please"
    )
    assert len(threads) == 1
    assert threads[0]["subreddit"] == "SaaS"
    assert threads[0]["thread_id"] == "xyz789"
    print("[ok] extractors")


def test_disabled_without_creds() -> None:
    """Confirms the tool disables itself silently when creds are missing —
    critical because we don't want it breaking runs for users who haven't
    set up a Reddit app yet."""
    reader = RedditReader(client_id="", client_secret="")
    assert not reader.enabled
    assert reader.read_for_task("research trends on r/marketing") == ""
    print("[ok] disabled without creds")


def test_live() -> None:
    reader = RedditReader()
    if not reader.enabled:
        print("[skip] live — REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set")
        return
    posts = reader.fetch_subreddit_top("indiehackers", window="week", limit=5)
    assert posts, "expected at least 1 post from r/indiehackers top-of-week"
    assert all(p.get("title") for p in posts)
    print(f"[ok] live — got {len(posts)} posts from r/indiehackers")

    block = reader.format_subreddit_for_prompt("indiehackers", posts)
    assert "REDDIT — TOP POSTS FROM r/indiehackers" in block
    print("---- PROMPT BLOCK PREVIEW ----")
    print(block[:800])
    print("------------------------------")


if __name__ == "__main__":
    test_triggers()
    test_extractors()
    test_disabled_without_creds()
    test_live()
    print("\nAll RedditReader checks passed.")
