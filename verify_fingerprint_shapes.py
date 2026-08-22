"""Run the REAL fingerprint JS over the table shapes that broke it.

A design review probed the first version in headless Chromium and found
two defects that no amount of reading would have shown, both caused by a
RANK / ROW-NUMBER column:

  - it is maximally distinct, so the old key rule never rejected it --
    and it reads 1,2,3... whatever order the rows are in, so the
    fingerprint said "the rows did not move" across a genuine re-sort.
  - it is also perfectly monotonic, so the sortedness scan reported
    "SORTED: # ascending" on a table nobody had touched, which would
    have passed the default view all over again.

This replays those shapes against the current code so the fix is
measured rather than asserted.

    python verify_fingerprint_shapes.py
"""
from __future__ import annotations

import sys

ROWS = [
    ("NVDA NVIDIA Corp", "225.16 USD", "−0.06%", "5.45 T USD"),
    ("AAPL Apple Inc.", "305.93 USD", "+0.22%", "4.46 T USD"),
    ("GOOG Alphabet", "343.54 USD", "−0.12%", "4.22 T USD"),
    ("MSFT Microsoft", "495.40 USD", "−0.30%", "3.68 T USD"),
    ("AMZN Amazon.com", "262.65 USD", "−0.94%", "2.83 T USD"),
    ("AVGO Broadcom", "392.99 USD", "−5.94%", "1.87 T USD"),
    ("META Meta Plat.", "589.85 USD", "−0.86%", "1.5 T USD"),
    ("TSLA Tesla, Inc.", "342.27 USD", "+0.68%", "1.35 T USD"),
    ("LLY Eli Lilly", "1,180.16 USD", "−2.39%", "1.11 T USD"),
    ("MU Micron Tech", "971.66 USD", "+2.30%", "1.1 T USD"),
]
HEAD = ("Symbol", "Price", "Chg %", "Mkt cap")


def _html(rows, mode):
    """mode: id | rank | checkbox | checkbox_rank | icon"""
    head, body = [], []
    for i, r in enumerate(rows, 1):
        cells, hs = list(r), list(HEAD)
        if mode in ("checkbox", "checkbox_rank"):
            cells.insert(0, ""); hs.insert(0, "")
        if mode in ("rank", "checkbox_rank"):
            at = 1 if mode == "checkbox_rank" else 0
            cells.insert(at, str(i)); hs.insert(at, "#")
        if mode == "icon":
            cells.insert(0, ""); hs.insert(0, "")
        if not head:
            head = ["<tr>" + "".join(f"<th>{h}</th>" for h in hs) + "</tr>"]
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return ("<html><body><table>" + head[0] + "".join(body)
            + "</table></body></html>")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    from playwright.sync_api import sync_playwright
    from backend.app.browser.observation import _DATA_JS

    ok = True
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        for mode in ("id", "rank", "checkbox", "checkbox_rank", "icon"):
            page.set_content(_html(ROWS, mode))
            before = page.evaluate(_DATA_JS) or {}
            # A genuine re-sort: same rows, different order.
            page.set_content(_html(list(reversed(ROWS)), mode))
            after = page.evaluate(_DATA_JS) or {}

            keys_b = (before.get("keys") or [])[:3]
            moved = (before.get("keys") or []) != (after.get("keys") or [])
            sorted_b = [c["column"] for c in (before.get("sorted") or [])]

            # A rank column must never be the key, and never be reported
            # as a sort — it is a position, not a measurement.
            good = moved and not any(c.strip() in ("#", "") for c in sorted_b)
            ok = ok and good
            print(f"  {mode:<14} keys={keys_b}")
            print(f"  {'':<14} detected re-sort={moved}  sorted_before={sorted_b}"
                  f"   {'OK' if good else 'FAIL'}")
        browser.close()

    print(f"\nALL SHAPES SOUND: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
