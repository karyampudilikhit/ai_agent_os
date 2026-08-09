"""fetch_market_data — get a real daily price series onto disk.

The gap this fills, measured: asked to build a quant model end to end,
a full run spent 5 specialists and 19 agentic steps and retrieved ZERO
rows of price data, because nothing in the tool registry could fetch
any. The planner reached for send_email, read_inbox and post_slack
instead — not because those made sense, but because they existed. The
deliverable then had no return, no Sharpe and no drawdown, and honestly
said so.

WHY THIS IS PYTHON AND NOT A CONFIG CONNECTOR. Everything new should be
config (see http_tool_store.py, and the Crossref/arXiv seeds that prove
it works). This one cannot be, for a concrete reason: HTTPToolRunner
truncates every response at MAX_RESPONSE_CHARS = 6000, and five years
of daily bars is well over 100KB. A config connector would hand the
model a price series cut off mid-row — worse than no data, because it
looks usable.

So the series is WRITTEN TO A FILE and the tool returns a summary plus
the path. That is also how an analyst actually works: fetch once,
compute many times. Pair it with run_python, which reads the CSV out of
the same workspace.

Source is Yahoo's chart endpoint: no API key, no signup, adjusted
closes included. Stooq was tried first and serves a JS-required
interstitial to non-browser clients.

Every fetch is recorded in the SourceLedger, so a deliverable citing
this data can be verified as sourced rather than remembered.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

from backend.app.actions.action_registry import ActionSpec

logger = logging.getLogger(__name__)

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TIMEOUT = 40.0
UA = "Mozilla/5.0 (compatible; vision-ai/0.1)"

VALID_RANGES = {"1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}
VALID_INTERVALS = {"1d", "1wk", "1mo"}
PREVIEW_ROWS = 3


class MarketDataError(Exception):
    pass


def _out_dir() -> str:
    try:
        from backend.app.actions.builtin._workspace import workspace_root
        root = os.path.join(str(workspace_root()), "market_data")
    except Exception:  # noqa: BLE001
        root = os.path.join(os.getcwd(), "market_data")
    os.makedirs(root, exist_ok=True)
    return root


def _handler(args: Dict[str, Any]) -> str:
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        raise MarketDataError("symbol is required, e.g. 'SPY' or 'AAPL'")

    rng = str(args.get("range") or "5y").strip().lower()
    interval = str(args.get("interval") or "1d").strip().lower()
    if rng not in VALID_RANGES:
        raise MarketDataError(
            f"range {rng!r} is not valid. Use one of: {', '.join(sorted(VALID_RANGES))}"
        )
    if interval not in VALID_INTERVALS:
        raise MarketDataError(
            f"interval {interval!r} is not valid. Use one of: "
            f"{', '.join(sorted(VALID_INTERVALS))}"
        )

    url = BASE.format(symbol=symbol)
    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}) as c:
            r = c.get(url, params={"range": rng, "interval": interval,
                                   "events": "div,split"})
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"could not reach the price API: {exc}")

    if r.status_code != 200:
        raise MarketDataError(
            f"price API returned HTTP {r.status_code} for {symbol!r}. Check the "
            f"ticker is real and correctly spelled (e.g. 'SPY', 'AAPL', "
            f"'^GSPC' for an index)."
        )

    try:
        payload = r.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            err = ((payload.get("chart") or {}).get("error") or {}).get("description")
            raise MarketDataError(
                f"no data returned for {symbol!r}"
                + (f": {err}" if err else " — check the ticker exists.")
            )
        block = result[0]
        stamps: List[int] = block.get("timestamp") or []
        quote = (block.get("indicators") or {}).get("quote") or [{}]
        adj = ((block.get("indicators") or {}).get("adjclose") or [{}])[0]
        o, h, l, c_, v = (quote[0].get(k) or [] for k in
                          ("open", "high", "low", "close", "volume"))
        adjclose = adj.get("adjclose") or []
    except MarketDataError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"could not parse the price response: {exc}")

    if not stamps:
        raise MarketDataError(f"no rows returned for {symbol!r} over range {rng}")

    path = os.path.join(_out_dir(), f"{symbol}_{rng}_{interval}.csv")
    written = 0
    try:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close", "adj_close", "volume"])
            for i, ts in enumerate(stamps):
                # Yahoo pads arrays with nulls for non-trading stamps —
                # skipping them keeps the CSV clean for pandas rather
                # than seeding it with NaNs a backtest would silently
                # propagate.
                if i >= len(c_) or c_[i] is None:
                    continue
                day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                w.writerow([
                    day,
                    _num(o, i), _num(h, i), _num(l, i), _num(c_, i),
                    _num(adjclose, i), _num(v, i),
                ])
                written += 1
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"could not write the CSV: {exc}")

    if not written:
        raise MarketDataError(f"every row for {symbol!r} was empty — no usable data")

    try:
        from backend.app.tools.source_ledger import get_ledger
        get_ledger().record_fetched(str(r.url))
    except Exception:  # noqa: BLE001
        pass

    first_day = datetime.fromtimestamp(stamps[0], tz=timezone.utc).strftime("%Y-%m-%d")
    last_day = datetime.fromtimestamp(stamps[-1], tz=timezone.utc).strftime("%Y-%m-%d")
    preview = _preview(path)
    return (
        f"Fetched {written} {interval} bars for {symbol} "
        f"({first_day} to {last_day}) from {r.url}\n"
        f"Saved to: {path}\n"
        f"Columns: date, open, high, low, close, adj_close, volume\n\n"
        f"First rows:\n{preview}\n\n"
        f"The full series is NOT printed here — it is on disk. Load and "
        f"compute on it with run_python, e.g.:\n"
        f"  import pandas as pd\n"
        f"  df = pd.read_csv(r'{path}', parse_dates=['date']).set_index('date')\n"
        f"  print(df['adj_close'].pct_change().std())"
    )


def _num(seq: List[Any], i: int) -> str:
    try:
        v = seq[i]
        return "" if v is None else f"{v:.6f}" if isinstance(v, float) else str(v)
    except Exception:  # noqa: BLE001
        return ""


def _preview(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return "".join(fh.readline() for _ in range(PREVIEW_ROWS + 1)).rstrip()
    except Exception:  # noqa: BLE001
        return "(preview unavailable)"


SPEC = ActionSpec(
    name="fetch_market_data",
    description=(
        "Download a REAL daily/weekly/monthly price history for a ticker and "
        "save it as a CSV you can compute on. Use this for ANY task needing "
        "market prices — backtests, volatility, correlations, returns — "
        "instead of recalling prices from memory, which produces confidently "
        "wrong numbers. Works for stocks, ETFs and indices (SPY, AAPL, "
        "^GSPC). Returns a summary and the file path, not the whole series; "
        "read the CSV with run_python to do the analysis."
    ),
    parameters=[
        {"name": "symbol", "type": "string",
         "description": "Ticker, e.g. 'SPY', 'AAPL', '^GSPC'.", "required": True},
        {"name": "range", "type": "string",
         "description": "History length: 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max. "
                        "Default 5y.", "required": False},
        {"name": "interval", "type": "string",
         "description": "Bar size: 1d, 1wk or 1mo. Default 1d.", "required": False},
    ],
    handler=_handler,
    preview=lambda a: f"Fetch {a.get('symbol')} prices ({a.get('range') or '5y'})",
    mutating=False,
    planner_excluded=False,
    capability="data.market.prices",
)
