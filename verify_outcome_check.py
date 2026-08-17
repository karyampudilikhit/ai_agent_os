"""Prove the outcome check against a real page, through the real code.

Opens a live table, records the fingerprint, sorts it the way the agent
would, and records the fingerprint again. No model involved — this tests
whether the MEASUREMENT is sound, which has to be true before anything
built on it can be.

The reason it exists: the first version of this design compared "did the
same rows come back in a different order", and that was wrong in a way
only a live page revealed — a 100-row table sampled 14 deep has every
sampled row REPLACED by a sort, so a genuine sort and a filter look
identical. Measuring monotonicity of the numbers is what a sort means.

    python verify_outcome_check.py
    python verify_outcome_check.py --url https://... --column Price
"""
from __future__ import annotations

import argparse
import sys

URL = "https://www.tradingview.com/screener/"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--column", default="Chg", help="header text to sort by")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    from backend.app.browser.observation import (
        observe_page, parse_data_keys, parse_sorted_columns, rows_changed,
    )
    from backend.app.browser.primitives import _click_locator
    from backend.app.browser.session_manager import get_manager, run_on_browser_thread

    mgr = get_manager()
    # create() MUST run on the browser thread as well, not merely the
    # calls after it. A Page built on the main thread belongs to that
    # thread's greenlet, and touching it from the executor raises
    # "Cannot switch to a different thread" — which names the symptom and
    # not the cause. This is the constraint browser/README.md warns about,
    # and it caught this script on the first run.
    session = run_on_browser_thread(
        lambda: mgr.create(args.url, prefer_headless=False), timeout=180)
    print(f"opened {args.url}\n")

    def snapshot():
        return observe_page(session.page, session.element_map).render(
            include_text=False)

    def wait_for_table():
        try:
            session.page.wait_for_selector("table tbody tr", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        session.page.wait_for_timeout(1500)
        return "table present"

    run_on_browser_thread(wait_for_table, timeout=60)
    before = run_on_browser_thread(snapshot, timeout=120)
    print("=" * 72)
    print("BEFORE")
    print("=" * 72)
    for line in before.splitlines():
        if line.startswith(("DATA:", "SORTED:")):
            print(line[:300])

    def do_sort():
        # Exactly what browser_click_element does, on the header cell the
        # observer now exposes.
        loc = session.page.locator(
            f"th:has-text('{args.column}')").first
        if not loc.count():
            return f"(no header matching {args.column!r})"
        _click_locator(session, loc, role="columnheader")
        session.page.wait_for_timeout(2500)
        return "clicked"

    print("\n" + run_on_browser_thread(do_sort, timeout=120))

    after = run_on_browser_thread(snapshot, timeout=120)
    print("\n" + "=" * 72)
    print("AFTER")
    print("=" * 72)
    for line in after.splitlines():
        if line.startswith(("DATA:", "SORTED:")):
            print(line[:300])

    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)
    changed = rows_changed(before, after)
    sorted_before = parse_sorted_columns(before) or []
    sorted_after = parse_sorted_columns(after) or []
    print(f"  rows changed          : {changed}")
    print(f"  sorted columns before : {sorted_before}")
    print(f"  sorted columns after  : {sorted_after}")
    print(f"  keys before           : {(parse_data_keys(before) or [])[:4]}")
    print(f"  keys after            : {(parse_data_keys(after) or [])[:4]}")
    # NOT "is something sorted" — the page ARRIVES sorted by market cap,
    # so that question answers yes on the very view this check exists to
    # reject. What proves the agent produced the ranking is that the
    # ordering CHANGED.
    from backend.app.browser.observation import sort_changed
    moved = sort_changed(before, after)
    print(f"  sort changed          : {moved}")
    ok = bool(changed) and bool(moved)
    print(f"\n  MEASUREMENT SOUND     : {ok}")
    if not ok:
        print("  (expected: rows changed True AND the sorted column changed)")

    mgr.close(session.token)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
