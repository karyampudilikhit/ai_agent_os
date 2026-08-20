"""Has the TASK finished, or merely the last action?

THE FAILURE THIS ANSWERS. Asked to open a page, follow a link, and go
back, a live run did exactly that in its first three calls -- and then
made twelve more. It found its way to "See also", scrolled, searched the
page, clicked around, and ended where it had already been at call three.
Every one of those calls succeeded. Not one of them advanced the task.

The loop could tell that an ACTION had worked. Nothing could tell that
the GOAL had been reached, so "success" kept reading as "carry on".

WHAT COUNTS AS EVIDENCE. Not the model saying DONE -- this codebase does
not ask a model whether its own work went well. What counts is the run
having OBSERVED the states the task asked for, in the ledger, in order.
A task naming three pages is satisfied when three page views carrying
those pages have actually been recorded; a task naming one is satisfied
when that one has. Requirements are read from the founder's own words and
checked against what the tools returned.

DELIBERATELY NARROW. This fires only when the task states its finish line
in terms a page view can verify -- a URL, a page name, a back-navigation.
Open-ended work ("research X and write it up") has no such line and is
left entirely to the existing DONE handling, because a completion check
that guesses would end runs early, which is a worse failure than ending
them late.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A URL written in the task itself. These are the finish lines a founder
# states most often and the ones a page view can check exactly.
_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+", re.IGNORECASE)

# "go back", "return to", "navigate back" -- a stated round trip. Present
# tense and imperative both, because tasks are written either way.
_BACK_RE = re.compile(
    r"\b(go|going|navigate|navigating|head|return|returning)\s+back\b"
    r"|\bback\s+to\s+the\b|\breturn\s+to\s+the\b",
    re.IGNORECASE,
)

_URL_LINE_RE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)
_NOW_ON_RE = re.compile(r"Now on:.*?\((\S+?)\)")
_WENT_BACK_RE = re.compile(r"Went back to (\S+)")


def _norm_url(url: str) -> str:
    """Compare pages, not decoration. A trailing slash, a fragment and a
    scheme difference are the same page to a reader and to this check."""
    # Lowercased FIRST. Doing it last meant "WWW.Example.com" never
    # matched the startswith("www.") test, so the same page written two
    # ways read as two pages.
    u = (url or "").strip().rstrip(".,);]'\"").lower()
    u = re.sub(r"^https?://", "", u)
    u = u.split("#", 1)[0]
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


_TITLE_RE = re.compile(r"^TITLE:\s*(.+)$", re.MULTILINE)
_NOW_ON_TITLE_RE = re.compile(r'Now on:\s*"([^"]*)"')

# A page named in words rather than as a URL. Real tasks say "open the
# Python page", not "open https://...", and a completion check that only
# understood URLs stayed silent on exactly the runs that needed it --
# measured on four of five acceptance tests.
_NAMED_PAGE_RES = (
    # "page for X", "article on X". FIRST, because "the page for the
    # Python programming language" also contains the shorter "the X
    # page" shape, and the longer name is the one the task means.
    re.compile(
        r"(?:page|article|entry)\s+(?:for|on|about)\s+(?:the\s+)?"
        r"([A-Za-z0-9][^.;:\n]{2,59}?)(?=\s*[.;:\n]|\s+(?:and|then|using)\s)",
        re.IGNORECASE,
    ),
    # "the X page", "the X article" — the commonest phrasing by far.
    re.compile(r"the\s+([A-Za-z0-9][^.;:\n]{2,59}?)\s+(?:page|article|entry)",
               re.IGNORECASE),
    # A quoted proper name: 'a link to the "Programming language" article'.
    re.compile("[\"“]([^\"”]{3,60})[\"”]"),
)

# Words that make a phrase a description of an action rather than a page
# name: "the browser's back action", "the History section".
_NOT_A_PAGE = (
    # Parts of a page, or things you do to one — never a page.
    "section", "action", "button", "link", "field", "table",
    "search", "browser", "url", "heading", "list", "menu", "title",
    # Deictic words. "returned you to the SAME page as stage 1" yielded
    # the page name "same", which no title can ever match — and since
    # every named page must be reached, one bogus name blocks the whole
    # completion check.
    "same", "that", "this", "these", "those", "each", "every", "any",
    "other", "another", "next", "previous", "following", "above", "below",
    "first", "second", "third", "final", "last", "stage", "step",
)


def _norm_name(text: str) -> str:
    """Compare page names the way a reader would: letters and spaces."""
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return " ".join(t.split())


def named_pages(task: str) -> List[str]:
    """Pages the task names in words. Never a substitute for a URL when
    one is given -- an addition, for the tasks that give neither."""
    out, seen = [], set()
    for rx in _NAMED_PAGE_RES:
        for m in rx.finditer(task or ""):
            name = _norm_name(m.group(1))
            if not name or len(name) < 3:
                continue
            if any(w in name.split() for w in _NOT_A_PAGE):
                continue
            if name not in seen:
                seen.add(name)
                out.append(name)

    # "the page for the Python programming language" yields both "python
    # programming language" and "python". They are one requirement, and
    # keeping both would demand two separate arrivals for one page.
    # Longest first, then drop anything contained in one already kept.
    # A WORD-PREFIX is the same page referred to again; a suffix is not.
    #
    # "the page for the Python programming language" (step 1) and "the
    # Python page" (step 3) are one page, and "python" is a word-prefix
    # of "python programming language" — so it goes. But "Programming
    # language" (step 2) is a DIFFERENT article that merely ends with
    # the same words, and dropping it would let the run finish without
    # ever visiting it. Containment alone conflated the two.
    kept: List[str] = []
    for name in sorted(out, key=lambda n: len(n.split()), reverse=True):
        words = name.split()
        if not any(k.split()[:len(words)] == words for k in kept):
            kept.append(name)
    return [n for n in out if n in kept]


def visited_titles(ledger_calls: Iterable[Dict[str, Any]]) -> List[str]:
    """The title of every page this run landed on, in order."""
    out: List[str] = []
    for call in sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0)):
        if not call.get("ok"):
            continue
        text = str(call.get("output") or call.get("result_preview") or "")
        m = _TITLE_RE.search(text) or _NOW_ON_TITLE_RE.search(text)
        if m:
            t = _norm_name(m.group(1))
            if t:
                out.append(t)
    return out


def _name_reached(want: str, titles: List[str]) -> bool:
    """A page whose title carries every word of the name. "python
    programming language" is reached by "python programming language
    wikipedia"; "programming language" is reached by "general purpose
    programming language wikipedia"."""
    words = want.split()
    return any(all(w in t.split() for w in words) for t in titles)


def urls_in(text: str) -> List[str]:
    seen, out = set(), []
    for raw in _URL_RE.findall(text or ""):
        n = _norm_url(raw)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _view_url(view: str) -> str:
    m = _URL_LINE_RE.search(view or "") or _NOW_ON_RE.search(view or "")
    return _norm_url(m.group(1)) if m else ""


def visited_urls(ledger_calls: Iterable[Dict[str, Any]]) -> List[str]:
    """Every page this run actually LANDED on, in order.

    Read from tool output rather than from the arguments a call was made
    with: asking for a URL is not the same as arriving at it, and the
    difference is the whole point of checking.
    """
    out: List[str] = []
    for call in sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0)):
        if not call.get("ok"):
            continue
        text = str(call.get("output") or call.get("result_preview") or "")
        m = _WENT_BACK_RE.search(text)
        if m:
            out.append(_norm_url(m.group(1)))
            continue
        u = _view_url(text)
        if u:
            out.append(u)
    return out


def went_back(ledger_calls: Iterable[Dict[str, Any]]) -> bool:
    """A back-navigation the BROWSER performed, not one the model claims."""
    for call in ledger_calls or ():
        if not call.get("ok"):
            continue
        if "browser_back" not in str(call.get("tool") or ""):
            continue
        if _WENT_BACK_RE.search(str(call.get("output")
                                    or call.get("result_preview") or "")):
            return True
    return False


def requirements(task: str) -> Dict[str, Any]:
    """The finish line, in terms a page view can check.

    Empty when the task does not state one — the common case, and the
    reason this is safe to run on every task.
    """
    urls = urls_in(task)
    names = [] if urls else named_pages(task)
    return {"urls": urls, "names": names,
            "needs_back": bool(_BACK_RE.search(task or ""))}


def check(task: str, ledger_calls: Iterable[Dict[str, Any]]) -> Tuple[bool, str]:
    """(complete, why). `complete` is True only on observed evidence.

    False is the safe answer and the default: an unverifiable goal is not
    a finished one, and the existing DONE path still governs those.
    """
    req = requirements(task)
    if not req["urls"] and not req["names"]:
        return False, ""

    calls = list(ledger_calls or ())
    visited = visited_urls(calls)
    titles = visited_titles(calls)
    if not visited and not titles:
        return False, ""

    # Pages named in words. Checked against the TITLE the browser
    # reported, so "the Python page" is satisfied by actually landing on
    # a page titled Python — never by the model saying it did.
    if req["names"]:
        # DISTINCT NAMES NEED DISTINCT ARRIVALS.
        #
        # "Programming language" is a word-subset of the title "Python
        # (programming language)", so a loose match let ONE page satisfy
        # BOTH names and the run finished without ever opening the
        # second article. Each name is therefore assigned its own page,
        # most specific name first so the longer one claims the title it
        # really belongs to.
        # DISTINCT PAGES, not distinct arrivals: a back-navigation
        # reports the starting page's title a second time, and counting
        # that as a second page let the run finish having opened only
        # one of the two the task named.
        unclaimed = list(dict.fromkeys(titles))
        reached_names = []
        for name in sorted(req["names"], key=lambda n: len(n.split()),
                           reverse=True):
            hit = next((t for t in unclaimed if _name_reached(name, [t])), None)
            if hit is not None:
                unclaimed.remove(hit)
                reached_names.append(name)
        if len(reached_names) < len(req["names"]):
            return False, ""
        if req["needs_back"]:
            if not went_back(calls):
                return False, ""
            # A round trip ends where it started.
            if not titles or not _name_reached(req["names"][0], titles[-1:]):
                return False, ""
        why = f"landed on all {len(reached_names)} page(s) the task named"
        if req["needs_back"]:
            why += ("; performed the back-navigation and ended on the "
                    "starting page")
        return True, why

    # A stated URL counts as reached if any page view landed on it or
    # inside it -- "wikipedia.org/wiki/Python" is reached by
    # "wikipedia.org/wiki/Python_(programming_language)" only if the task
    # named the shorter form, never the reverse.
    reached = []
    for want in req["urls"]:
        if any(v == want or v.startswith(want + "/") or want in v for v in visited):
            reached.append(want)
    if len(reached) < len(req["urls"]):
        return False, ""

    if req["needs_back"] and not went_back(calls):
        return False, ""

    # A round trip is only finished back at the start.
    if req["needs_back"] and visited and req["urls"]:
        first = req["urls"][0]
        last = visited[-1]
        if not (last == first or first in last):
            return False, ""

    parts = [f"reached {len(reached)} of the page(s) the task named"]
    if req["needs_back"]:
        parts.append("performed the back-navigation and ended on the "
                     "starting page")
    return True, "; ".join(parts)
