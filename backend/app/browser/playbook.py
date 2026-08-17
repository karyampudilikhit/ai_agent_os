"""What worked last time on this site.

THE FAILURE THIS ANSWERS. The winning path through TradingView's screener
is three moves: open the Performance tab, find the "Perf % 1W" column,
click it. The agent discovered that path ONCE in five attempts, and
forgot it every single time -- every run started from nothing and
re-explored a site that had already been solved. A task that works one
time in five stays one in five forever.

So a run that PASSES THE GATE has its path distilled and kept. The next
run on the same site gets handed the recipe. Discovery happens once;
after that it is a replay.

This matters most for exactly the work founders repeat -- the same
outreach flow, the same report, the same board, every week. The first run
is exploration and the rest are execution.

WHAT IS STORED IS SEMANTIC, NOT LITERAL. Element ids are per-observation
by design: `e59` means nothing an hour later, and a playbook full of them
would be a machine for clicking the wrong thing with confidence. What
gets kept is what a person would write down -- `click "Perf % 1W"`,
`search for "weekly change column"` -- so the next run has to re-find the
control by name and can adapt when the page has moved on.

IT IS A HINT, NEVER A SCRIPT. The path is injected as prior knowledge and
the agent is told plainly that the site may have changed. Blind replay
would trade an honest failure for a confident wrong answer, which is the
trade this codebase exists to refuse. Every guard still runs: a replayed
path that no longer works fails exactly as loudly as a discovered one.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

STORE_FILENAME = "browser_playbooks.json"

# How many steps of a path are kept. Long enough for a real flow, short
# enough that the hint cannot crowd the step prompt.
MAX_STEPS_KEPT = 24
# How many playbooks are kept in total, newest first. A cap rather than
# unbounded growth, because a stale recipe is worse than none.
MAX_PLAYBOOKS = 200
# A recalled playbook has to be genuinely about the same job. Below this
# the match is noise, and a wrong recipe is worse than no recipe.
MIN_RELEVANCE = 0.28


def _store_path() -> Path:
    from backend.app.utils.paths import under_data
    return under_data(STORE_FILENAME)


# What each browser tool contributes to a written-down recipe.
_CLICKED = re.compile(r'Clicked\s+\S+\s+"([^"]{1,60})"')
_SELECTED = re.compile(r"Selected\s+'([^']{1,60})'|Chose\s+\"([^\"]{1,60})\"")
_NOW_ON = re.compile(r"Now on:.*?\((\S+?)\)")
_URL_LINE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)


def domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    return host[4:] if host.startswith("www.") else host


def _args(call: Dict[str, Any]) -> Dict[str, Any]:
    try:
        parsed = json.loads(call.get("args_text") or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def distil(ledger_calls) -> Dict[str, Any]:
    """Turn a run's browser calls into a recipe a person could follow.

    Only SUCCEEDED calls, in order, and only the ones that carry meaning
    for a repeat: where it went, what it searched for, what it operated.
    Observing and scrolling are omitted -- they are how the agent looks
    around, not what it did.
    """
    steps: List[str] = []
    domains: List[str] = []
    entry_url = ""

    calls = sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0))
    for call in calls:
        if not call.get("ok"):
            continue
        tool = str(call.get("tool") or "")
        if "browser" not in tool:
            continue
        out = str(call.get("output") or call.get("result_preview") or "")
        args = _args(call)

        if "browser_navigate" in tool:
            url = str(args.get("url") or "")
            if not url:
                m = _NOW_ON.search(out) or _URL_LINE.search(out)
                url = m.group(1) if m else ""
            if not url:
                continue
            d = domain_of(url)
            if d and d not in domains:
                domains.append(d)
            if not entry_url:
                entry_url = url
            steps.append(f'go to {url}')

        elif "browser_find" in tool:
            q = str(args.get("query") or "").strip()
            if q:
                steps.append(f'search the page for "{q}"')

        elif "browser_click" in tool:
            m = _CLICKED.search(out)
            name = (m.group(1) if m else "").strip()
            # A click whose target has no name is not reusable: the next
            # run cannot find "the third unnamed button" again.
            if name:
                steps.append(f'click "{name}"')

        elif "browser_select" in tool:
            opt = str(args.get("option") or "").strip()
            if opt:
                steps.append(f'choose "{opt}"')

        elif "browser_type" in tool:
            # The TEXT is deliberately not stored -- it is this run's
            # data, often personal, and never the next run's.
            steps.append("type into the field")

        elif "browser_extract_table" in tool:
            steps.append("read the table")

        if len(steps) >= MAX_STEPS_KEPT:
            break

    return {"steps": steps, "domains": domains, "entry_url": entry_url}


class PlaybookStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _store_path()
        self._lock = threading.RLock()

    def _load(self) -> List[Dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("playbook store unreadable (%s) — starting empty", exc)
            return []
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
        return data if isinstance(data, list) else []

    def _save(self, items: List[Dict[str, Any]]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(items[:MAX_PLAYBOOKS], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not write playbook store: %s", exc)

    def record(self, task: str, ledger_calls, *, at: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Keep the path of a run that produced a usable deliverable.

        Called only after the gate passes, so a path is never learned
        from a run whose answer was refused. Learning from a failure
        would teach the agent to repeat it.
        """
        recipe = distil(ledger_calls)
        # A one-step path is "we opened a URL" -- true, and not worth
        # recalling. The value is in the moves after the first.
        if len(recipe["steps"]) < 2 or not recipe["domains"]:
            return None

        entry = {
            "task": (task or "").strip()[:400],
            "domains": recipe["domains"],
            "entry_url": recipe["entry_url"],
            "steps": recipe["steps"],
            "recorded_at": at if at is not None else time.time(),
        }
        with self._lock:
            items = self._load()
            # One playbook per (task, primary domain): the newest verified
            # path replaces the older one, because a site that changed is
            # better described by the run that just worked.
            key = (entry["task"].lower(), entry["domains"][0])
            items = [i for i in items
                     if (str(i.get("task", "")).lower(),
                         (i.get("domains") or [""])[0]) != key]
            items.insert(0, entry)
            self._save(items)
        logger.info("playbook recorded: %s — %d step(s)",
                    entry["domains"][0], len(entry["steps"]))
        return entry

    def recall(self, task: str) -> Optional[Dict[str, Any]]:
        """The best-matching stored path for this task, or None."""
        task = (task or "").strip()
        if not task:
            return None
        with self._lock:
            items = self._load()
        if not items:
            return None
        try:
            from backend.app.memory import retrieval
            docs = [(str(i), f"{i.get('task','')} {' '.join(i.get('domains') or [])}")
                    for i in items]
            ranked = retrieval.rank(task, docs, limit=1, min_relevance=MIN_RELEVANCE)
        except Exception:  # noqa: BLE001
            return None
        if not ranked:
            return None
        wanted = ranked[0][0]
        for i in items:
            if str(i) == wanted:
                return i
        return None

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._load()

    def clear(self) -> None:
        with self._lock:
            self._save([])


_store: Optional[PlaybookStore] = None
_store_lock = threading.RLock()


def get_store() -> PlaybookStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = PlaybookStore()
        return _store


def hint_for(task: str) -> str:
    """The block injected into a step prompt, or "" when nothing matches.

    Worded as prior knowledge that may be stale, never as instructions.
    An agent that follows a dead recipe and reports success is the exact
    failure the rest of this layer exists to prevent, so the hint says
    plainly what to do when the page no longer matches.
    """
    try:
        found = get_store().recall(task)
    except Exception:  # noqa: BLE001
        return ""
    if not found:
        return ""
    lines = "\n".join(f"  {n}. {s}" for n, s in enumerate(found["steps"], 1))
    return (
        "\nWHAT WORKED LAST TIME ON THIS SITE — a path from an earlier run "
        "of a task like this one, which produced a verified answer:\n"
        f"{lines}\n"
        "Treat this as a starting point, not a script. The site may have "
        "changed, the names may differ, and the element ids from that run "
        "are long gone — you still have to find each control yourself and "
        "confirm each step did what you expected. If the page does not "
        "match this path, abandon it and work the page in front of you.\n"
    )
