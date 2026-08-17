"""Prove the login and overlay handling against real pages.

Both of these fail silently when they are wrong, which is why they get
measured rather than reasoned about:

  - a consent wall swallows every click while the content sits visible
    underneath, so the agent sees "the page didn't move" and blames its
    own element choice. It then spends the whole budget clicking through
    an overlay it cannot see.
  - a login wall looks like an ordinary form. Without detection the agent
    tries to fill it, which is both useless and the one thing this system
    must never do.

    python verify_login_and_overlay.py
"""
from __future__ import annotations

import sys

# Real pages that reliably show the thing being tested.
OVERLAY_SITES = [
    "https://www.theguardian.com/international",   # CMP consent wall
    "https://www.bbc.com/news",
]
LOGIN_SITES = [
    "https://github.com/login",
    "https://www.linkedin.com/login",
]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    from backend.app.browser.primitives import (
        _dismiss_impl, _on_login_wall, _OVERLAY_JS,
    )
    from backend.app.browser.session_manager import get_manager, run_on_browser_thread

    mgr = get_manager()
    ok = True

    print("=" * 72)
    print("LOGIN WALL DETECTION")
    print("=" * 72)
    for url in LOGIN_SITES:
        session = run_on_browser_thread(
            lambda u=url: mgr.create(u, prefer_headless=True), timeout=180)
        try:
            run_on_browser_thread(
                lambda: session.page.wait_for_timeout(2500), timeout=60)
            found = run_on_browser_thread(
                lambda: _on_login_wall(session.page), timeout=60)
            print(f"  {'DETECTED' if found else 'MISSED  '}  {url}")
            ok = ok and found
        finally:
            mgr.close(session.token)

    print()
    print("=" * 72)
    print("OVERLAY DISMISSAL")
    print("=" * 72)
    for url in OVERLAY_SITES:
        session = run_on_browser_thread(
            lambda u=url: mgr.create(u, prefer_headless=True), timeout=180)
        try:
            run_on_browser_thread(
                lambda: session.page.wait_for_timeout(4000), timeout=60)
            before = run_on_browser_thread(
                lambda: session.page.evaluate(_OVERLAY_JS) or [], timeout=60)
            # MUST run on the browser thread — it touches the Page, and
            # calling it from here raises "Cannot switch to a different
            # thread", which names the symptom and not the cause.
            out = run_on_browser_thread(
                lambda: _dismiss_impl({"session_token": session.token}),
                timeout=180)
            after = run_on_browser_thread(
                lambda: session.page.evaluate(_OVERLAY_JS) or [], timeout=60)
            headline = out.splitlines()[0][:90]
            print(f"  {url}")
            print(f"     overlays before: {len(before)}   after: {len(after)}")
            print(f"     -> {headline}")
            # Either it closed something, or it correctly said there was
            # nothing to close. Both are right; claiming to have dismissed
            # a popup that was never there is not.
            honest = (len(after) < len(before)) or ("Nothing is covering" in out) \
                or ("Could not close" in out)
            if not honest:
                ok = False
                print("     ^ NOT HONEST about what it did")
        finally:
            mgr.close(session.token)

    print()
    print(f"BOTH SOUND: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
