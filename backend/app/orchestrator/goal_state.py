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


def _nav_complete(task: str,
                  ledger_calls: Iterable[Dict[str, Any]]) -> Tuple[bool, str]:
    """(complete, why) for the part of a finish line a PAGE VIEW can check.

    The original check, moved and otherwise untouched. It is now one of
    three ways a goal can be finished rather than the only one; see
    `check` at the bottom of this module.

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


# ------------------------------------------------------- content goals
#
# THE FAILURE THIS ANSWERS. Asked for the first five entries of a page's
# table of contents, a live run got all five at call two and then made
# TEN MORE. It had the answer in hand and no way to know it, because the
# only finish line this module understood was "did we land on the pages
# the task named" -- and it had landed on the one page there was.
#
# NAVIGATION IS NOT THE WHOLE FINISH LINE. Worse, for that same task the
# navigation check would have said DONE the moment the page opened,
# BEFORE the contents were read. "Go to X and give me Y" needs both. So
# a content requirement makes completion strictly harder, never easier:
# arriving still counts for nothing until the asked-for content is in
# hand.
#
# COUNTED FROM OUR OWN TOOLS' OWN COUNT LINES. Each extractor already
# prints how much it returned -- "12 entr(y/ies)", "40 heading(s)",
# "showing 25", "DATA: 30 row(s)". Those lines are written by this
# codebase, so reading them is reading a record. Nothing here counts
# words on a page, and nothing asks the model how much it got.

_TOC_COUNT_RE = re.compile(r"(\d+)\s+entr\(y/ies\)")
_HEADING_COUNT_RE = re.compile(r"(\d+)\s+heading\(s\)")
_RECORDS_COUNT_RE = re.compile(r"similar item\(s\) on the page,\s*showing\s+(\d+)")
_ROWS_RE = re.compile(r"^DATA:\s*(\d+) row\(s\)", re.MULTILINE)

# Longest tool name first: "browser_extract_toc" also contains
# "browser_extract", and the first match wins.
_COUNTING_TOOLS = (
    ("browser_extract_records", _RECORDS_COUNT_RE),
    ("browser_extract_table", _ROWS_RE),
    ("browser_extract_toc", _TOC_COUNT_RE),
    ("browser_outline", _HEADING_COUNT_RE),
)


def content_reads(ledger_calls: Iterable[Dict[str, Any]]
                  ) -> List[Tuple[float, int, str]]:
    """(when, how many, what it returned) for every counted read.

    The TEXT matters as much as the count. Field evidence has to be
    looked for in the very content that produced the entries, not
    anywhere in the run: checked run-wide, a search snippet mentioning
    the word "product" vouched for per-company product data on a listing
    that had none, and an unrelated external link in a search result
    vouched for every company's website. Both observed on a real trace.
    """
    out: List[Tuple[float, int, str]] = []
    for call in sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0)):
        if not call.get("ok"):
            continue
        tool = str(call.get("tool") or "")
        rx = next((r for name, r in _COUNTING_TOOLS if name in tool), None)
        if rx is None:
            continue
        text = str(call.get("output") or call.get("result_preview") or "")
        m = rx.search(text)
        if not m:
            continue
        try:
            out.append((float(call.get("at") or 0), int(m.group(1)), text))
        except (TypeError, ValueError):
            continue
    return out


def content_entries(ledger_calls: Iterable[Dict[str, Any]]
                    ) -> List[Tuple[float, int]]:
    """(when, how many) for every read that reported its own count.

    The DATA: row count is read only from a TABLE EXTRACTION. It also
    appears in the page fingerprint attached to navigations and clicks,
    where it means "this page HAS a table" rather than "we read one" --
    counting those would let a run finish on content it never took.
    """
    out: List[Tuple[float, int]] = []
    for call in sorted(ledger_calls or (), key=lambda c: float(c.get("at") or 0)):
        if not call.get("ok"):
            continue
        tool = str(call.get("tool") or "")
        rx = next((r for name, r in _COUNTING_TOOLS if name in tool), None)
        if rx is None:
            continue
        text = str(call.get("output") or call.get("result_preview") or "")
        m = rx.search(text)
        if not m:
            continue
        try:
            out.append((float(call.get("at") or 0), int(m.group(1))))
        except (TypeError, ValueError):
            continue
    return out


# A FIELD IS NOT PRESENT BECAUSE ITS NAME APPEARS IN A URL.
#
# Inc42's listing links carry "itm_medium=website" in every query
# string. Matching field names against the raw text made "website" look
# evidenced on a page that never showed one company's own address. So
# URLs come out before names are looked for, and the fields that ARE
# URLs get checked against the URLs themselves.
_URL_ANY = re.compile(r"https?://\S+")
_URL_FIELDS = {"website", "url", "link", "homepage"}

# What a field may be called on a real page. Deliberately short: every
# synonym is a way for a field to look present when it is not, and a
# false "evidenced" is the failure this whole check exists to stop.
_FIELD_SYNONYMS = {
    "funding": ("funding", "funded", "raised", "investment", "valuation"),
    "product": ("product", "offering", "solution", "description",
                "what they do", "what it does"),
    "pricing": ("pricing", "price", "cost", "/mo", "per month", "per user"),
    "price": ("price", "pricing", "cost", "/mo", "per month"),
    "date": ("date", "posted", "published", "founded", "updated"),
    "location": ("location", "based in", "headquarter", "city", "office"),
    "salary": ("salary", "compensation", "pay", "ctc"),
    "stipend": ("stipend", "salary", "pay"),
    "company": ("company", "employer", "organisation", "organization", "firm"),
    "role": ("role", "position", "title", "job"),
}


def fields_evidenced(required: Iterable[str], captured: str,
                     source_hosts: Iterable[str] = ()) -> Tuple[List[str], List[str]]:
    """(present, missing) for the fields the task named.

    A field counts as evidenced when its own name -- or one of the few
    things a real page calls it -- appears in what the run actually
    read. A field that IS a URL counts only when a URL appears that is
    not on the site the list came from: an aggregator linking to its own
    profile pages has not given you anybody's website.

    Never guesses at meaning. It cannot tell which sentence is the
    product description, and does not pretend to -- it can only tell
    whether anything on the page was labelled as one.
    """
    text = captured or ""
    urls = _URL_ANY.findall(text)
    prose = _URL_ANY.sub(" ", text).lower()
    hosts = {h.lower() for h in (source_hosts or ()) if h}

    present: List[str] = []
    missing: List[str] = []
    for raw in required or ():
        field = str(raw or "").strip().lower()
        if not field:
            continue
        if field in _URL_FIELDS:
            foreign = [u for u in urls
                       if not any(h and h in u.lower() for h in hosts)]
            (present if foreign else missing).append(field)
            continue
        names = _FIELD_SYNONYMS.get(field, (field,))
        hit = any(re.search(r"\b" + re.escape(n) + r"\b", prose)
                  if n.isalpha() else n in prose
                  for n in names)
        (present if hit else missing).append(field)
    return present, missing


def _host_of(url: str) -> str:
    m = re.match(r"(?:https?://)?(?:www\.)?([^/\s]+)", (url or "").strip())
    return m.group(1).lower() if m else ""


# The headers OUR OWN extractors write to say where content came from.
_SOURCE_HEADERS = (
    re.compile(r"^Source:\s*(\S+)", re.MULTILINE),
    re.compile(r"^\[[^\]\n]*?(https?://[^\s\]]+)\]", re.MULTILINE),
    re.compile(r"^URL:\s*(\S+)", re.MULTILINE),
)


def _source_urls(text: str) -> List[str]:
    out = []
    for rx in _SOURCE_HEADERS:
        out.extend(rx.findall(text or ""))
    return out


def content_requirement(spec: Any) -> int:
    """How many entries the answer must carry, or 0 for "no such line".

    Only a spec that is CONFIDENT about a count of things that are not
    each visited separately. "The first three sentences" is one answer,
    not three, and goal_spec already refuses sentence nouns as counts;
    per-item work is a different finish line, handled below.
    """
    if spec is None or not getattr(spec, "confident", False):
        return 0
    if getattr(spec, "per_item_work", False):
        return 0
    try:
        n = int(getattr(spec, "item_count", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return n if n >= 2 else 0


def item_requirement(spec: Any, target_items: int) -> int:
    """How many items must be opened and read, or 0 for "no such line".

    `target_items` is the plan's figure rather than the spec's, because
    a descoped run's finish line is the eight it was told to do properly,
    not the ten it could not afford.
    """
    if spec is None or not getattr(spec, "confident", False):
        return 0
    if not getattr(spec, "per_item_work", False):
        return 0
    try:
        n = int(target_items or 0)
    except (TypeError, ValueError):
        return 0
    return n if n >= 2 else 0


def _first_requirement_hit(req: Dict[str, Any],
                           calls: List[Dict[str, Any]]) -> float:
    """When this run first reached somewhere the task named. 0 if never,
    and 0 when the task named nowhere -- both mean "no threshold"."""
    if not req["urls"] and not req["names"]:
        return 0.0
    for call in sorted(calls, key=lambda c: float(c.get("at") or 0)):
        if not call.get("ok"):
            continue
        text = str(call.get("output") or call.get("result_preview") or "")
        at = float(call.get("at") or 0)
        u = _view_url(text)
        if u and any(u == w or u.startswith(w + "/") or w in u
                     for w in req["urls"]):
            return at
        m = _TITLE_RE.search(text) or _NOW_ON_TITLE_RE.search(text)
        if m:
            t = _norm_name(m.group(1))
            if t and any(_name_reached(n, [t]) for n in req["names"]):
                return at
    return 0.0


def check(task: str, ledger_calls: Iterable[Dict[str, Any]],
          spec: Any = None, target_items: int = 0) -> Tuple[bool, str]:
    """(complete, why). `complete` is True only on observed evidence.

    Three finish lines, all read from the ledger and none of them from
    the model's account of itself:

      navigation   the pages the task named were landed on
      content      an extractor returned at least the asked-for number
      items        that many candidates were each opened AND read

    `spec` and `target_items` are optional: without them this behaves
    exactly as it did when navigation was the only finish line, which is
    what every caller that has not been taught about goal shapes needs.
    """
    calls = list(ledger_calls or ())
    req = requirements(task)
    want_content = content_requirement(spec)
    want_items = item_requirement(spec, target_items)

    nav_ok, nav_why = _nav_complete(task, calls)
    if not want_content and not want_items:
        return nav_ok, nav_why

    # A stated page is still a requirement. Content obtained somewhere
    # else is not this task finished -- and the ordering matters as much
    # as the arrival, so a read that happened BEFORE the run got there
    # cannot satisfy it.
    stated_pages = bool(req["urls"] or req["names"])
    if stated_pages and not nav_ok:
        return False, ""
    after = _first_requirement_hit(req, calls) if stated_pages else 0.0

    if want_items:
        try:
            from backend.app.orchestrator.item_state import derive
            done = derive(calls, want_items).done
        except Exception:  # noqa: BLE001
            return False, ""
        if done < want_items:
            return False, ""
        why = f"opened and read all {want_items} item(s) the task asked for"
        return True, (f"{nav_why}; {why}" if nav_why else why)

    qualifying = [(n, text) for at, n, text in content_reads(calls)
                  if at >= after and n >= want_content]
    got = max((n for n, _ in qualifying), default=0)
    if got < want_content:
        return False, ""

    # ENOUGH ROWS IS NOT THE SAME AS THE ANSWER.
    #
    # A live run asked for "company, funding, product, and website"
    # found ten entries on one Inc42 list page and stopped, complete by
    # its own reckoning, having obtained company and funding and neither
    # of the other two. Counting entries answers "how many"; it says
    # nothing about "of what". Both were asked for.
    #
    # Missing fields do not fail the run -- they keep it going, which is
    # the safe direction: ending late costs steps, ending early costs
    # the founder the answer.
    required = list(getattr(spec, "per_item_fields", ()) or ())
    if required:
        # Only the content that produced the entries. See content_reads.
        best = max(qualifying, key=lambda p: p[0])[1]
        # The host the content CAME FROM, taken from the header our own
        # extractor wrote -- not every host mentioned in it. Using them
        # all would mean any external link on the page counted as its
        # own source, and "website" would be satisfied by the listing
        # linking anywhere at all.
        hosts = {_host_of(u) for u in _source_urls(best)}
        hosts |= {_host_of(u) for u in req["urls"]}
        hosts.discard("")
        _, missing = fields_evidenced(required, best, hosts)
        if missing:
            logger.info("[goal-state] %d entr(y/ies) read but nothing on that "
                        "page was labelled: %s", got, ", ".join(missing))
            return False, ""

    why = (f"read {got} entr(y/ies) on the page, covering the "
           f"{want_content} the task asked for")
    if required:
        why += f", with evidence for every field asked for ({', '.join(required)})"
    return True, (f"{nav_why}; {why}" if nav_why else why)
