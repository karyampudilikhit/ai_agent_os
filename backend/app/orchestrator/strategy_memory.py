"""Which APPROACH has already failed on which page, this run.

THE FAILURE THIS ANSWERS. A live screener run spent five of its calls
searching one page for a sort control, wording the search differently
every time -- "1 month performance column", "performance column header",
"sort by month", "monthly change column", "sort control". The page had
no such control. Every call returned NO MATCH, and every call was
allowed through.

Three guards were watching and none of them could see it:

  the repeat guard      fingerprints (tool, arguments), and all five
                        argument strings differed
  instrument memory     keys on host x instrument, and the browser was
                        working perfectly on that host -- it was reading
                        the page fine, there was simply nothing to find
  the failure streak    counts calls that ERRORED, and NO MATCH is a
                        successful call that answers the question

What none of them held is the thing a person would have noticed after
the second try: this is the same question, asked again, of a page that
has not changed.

INTENT, NOT WORDING. So the key here is (page, family, subject words) --
what is being looked for, stripped of the phrasing. "date filter" and
"filter by date posted" are one intent; "date filter" and "next page
button" are two. Matching is by PROPORTION of shared subject words, the
same measure playbook.py settled on, because a count of shared words
makes long descriptions match everything.

AND A SECOND RULE, BECAUSE INTENT ALONE DOES NOT COVER THAT RUN. Written
out, those five searches are three questions, not one -- the first two
cluster, the last two cluster, and the fourth joins the first pair only
once "monthly" and "month" stem together. An intent rule by itself would
have caught some of the waste and let the rest through. What the whole
sequence shows is simpler than any single match: this page has been
searched, exhaustively, and has answered nothing found every time. So a
page that has said no three times refuses the fourth search regardless
of what it asks. The cost of that being wrong is one findable control
missed on a page where three others were not; the message says how to
lift it, and any change to the page lifts it automatically.

WHAT IS RECORDED IS A FACT. An attempt is filed as failed only when one
of OUR OWN tools said it found nothing -- "NO MATCH.", "(no data table
on this page)", "NO REPEATED RECORDS FOUND." Every marker carries
punctuation or a tool name that a site cannot write, for the same reason
instrument_memory's do: a page that happens to contain the sentence "we
could not find anything matching your search" must not be able to file a
verdict about our own tools.

IT NOTES FIRST, AND ONLY REFUSES AN UNCHANGED PAGE. instrument_memory
nudges forever because a wall can come down mid-run. That reasoning does
not carry over here: browser_find reports how many elements it searched,
and its own message says rewording is unlikely to help. But a page CAN
change -- a tab opens, a menu expands, a scroll loads more -- and then
the same search is a genuinely new question. So the refusal is gated on
the page view being byte-identical to the one that already failed twice.
If anything moved, the attempt goes through with a note instead. That is
the same test the repeat guard already uses to decide whether an
identical call is really a repeat.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Shapes one of OUR tools uses to say "there is nothing here answering
# that". Not one of these is a sentence a site can put on a page: each
# carries a tool name or bracket punctuation we write ourselves.
_FAILED_MARKERS = (
    "NO MATCH. Nothing on this page answers that description.",
    "NO REPEATED RECORDS FOUND.",
    "(browser_click: could not find anything matching",
    "(browser_task: could not find any fields on this page matching",
    "(could not read records from this page:",
    "(browser_extract_toc failed",
    "(no headings found on",
)

# "No table here" IS one of our own messages, but it is also part of the
# page FINGERPRINT that every navigation, click and scroll carries. Left
# in the list above, an ordinary click on an ordinary page without a
# table would have been filed as a failed approach -- and three of them
# would have refused the fourth. So it counts only from the tool whose
# whole job was to read a table.
_TABLE_ONLY_MARKERS = (
    "DATA: (no data table on this page)",
    "DATA: (table present but empty)",
)
_TABLE_TOOL = "browser_extract_table"

# What KIND of question a tool asks. An approach that failed while
# locating a control says nothing about whether the page can be read, so
# verdicts never cross between families.
LOCATE = "locate"
READ = "read"
OPERATE = "operate"
SEARCH = "search"

_FAMILIES = (
    # SEARCH first: web_search must not be swallowed by a broader match.
    (SEARCH, ("web_search",)),
    (LOCATE, ("browser_find", "browser_observe")),
    (READ, ("browser_extract", "browser_extract_table",
            "browser_extract_records", "browser_extract_section",
            "browser_extract_toc", "browser_outline",
            "browser_page_structure")),
    (OPERATE, ("browser_click", "browser_click_element", "browser_select",
               "browser_type", "browser_press")),
)

# A SEARCH IS NOT SCOPED TO A PAGE.
#
# Every other family keys on the page the call was made against, because
# "is this control on THIS page" is a question about that page. A web
# search is a question about the whole web, and asking it twice from two
# different pages is still asking it twice. So searches share one scope.
#
# Without this, run 4 of the AI-startups task recorded nothing at all:
# its first searches happened before any browser page existed, so the
# page key was empty and the memory returned before doing anything.
SEARCH_SCOPE = "(the web)"

# AND A SEARCH THAT RETURNED RESULTS IS STILL A REPEAT.
#
# Every other family only remembers attempts that came back with
# NOTHING, because a control that was found is a question answered. A
# search engine is different: the corpus does not change between two
# identical questions, so asking again cannot produce what the first ask
# did not -- whether or not it returned rows.
#
# Measured on a live run, 2026-08-21: eleven web_search calls, every one
# successful, every one a rewording of "top 10 AI startups in India
# funding product website". Twenty-two steps spent, no source opened.
# The repeat guard saw eleven different argument strings; this module
# saw nothing at all, because web_search was in no family.
SEARCH_NOTE_AFTER = 1
SEARCH_REFUSE_AFTER = 4

# Words that describe HOW something is worded rather than WHAT is
# wanted. Dropping them is what makes "the sort dropdown" and "sort
# control" one intent. Kept deliberately short: every word removed is a
# way for two different intents to collapse into one, and a wrong
# collapse silences a search that should have run.
_STOP = {
    "a", "an", "the", "this", "that", "these", "those", "any", "all",
    "and", "or", "of", "for", "to", "on", "in", "at", "by", "with",
    "from", "into", "its", "it", "is", "are", "was", "be", "me", "my",
    "please", "find", "show", "get", "give", "look", "search", "locate",
    "element", "control", "widget", "component", "thing", "option",
    "page", "site", "website", "screen", "view",
}

# How much of the shorter description must be shared before two attempts
# count as the same question. Chosen so "date filter" matches "filter by
# date posted" (1.0) and not "sort by date" (0.5) -- deliberately strict,
# because a wrongly-collapsed intent silences a search that should have
# run, and that is the expensive direction of this mistake.
SAME_INTENT = 0.6

# One failed attempt at the same question earns a note; a second earns a
# refusal, and only then on a page that has not moved since.
NOTE_AFTER = 1
REFUSE_AFTER = 2

# AND A SECOND, COARSER RULE, because the strict one does not cover the
# run this module was built for. Written out, the five real screener
# searches cluster into three questions, not one:
#
#   "1 month performance column"  \_ 0.67 shared
#   "performance column header"   /
#   "monthly change column"        -- joins those two once monthly stems
#   "sort by month"               \_ 1.00 shared
#   "sort control"                /
#
# So an intent rule alone would have caught some of the waste and let the
# rest through. What the whole sequence demonstrates is simpler than any
# of the individual matches: this page has been searched four times and
# has answered NO MATCH every time. A page that is not answering searches
# is not answering searches, whatever is being asked of it.
# Three searches of every element on one page, three answers of nothing
# found, and the fourth is refused. The cost of being wrong here is one
# control that WAS on the page going unfound on a run where three others
# were not -- and the refusal message says exactly how to lift it: change
# the view, or read what the page does show. Set against five calls of a
# twenty-two call budget, that is the right side to err on.
PAGE_NOTE_AFTER = 2
PAGE_REFUSE_AFTER = 3


def family_of(tool: str) -> str:
    name = (tool or "").lower()
    for fam, tools in _FAMILIES:
        if any(t in name for t in tools):
            return fam
    return ""


def looks_fruitless(result: str, tool: str = "") -> bool:
    """Did one of our own tools report finding nothing?

    `tool` gates the markers that are only meaningful from one tool. Its
    default is the strict reading -- an unknown caller gets only the
    messages that mean the same thing wherever they appear.
    """
    text = result or ""
    if any(m in text for m in _FAILED_MARKERS):
        return True
    return (_TABLE_TOOL in (tool or "")
            and any(m in text for m in _TABLE_ONLY_MARKERS))


def _norm_page(url: str) -> str:
    """Two addresses are one page when a reader would call them one.

    The QUERY STRING IS KEPT, unlike elsewhere in this codebase: a sort
    or filter applied through the address bar is exactly the change that
    makes a second search worth running, and dropping it would let this
    memory refuse the one attempt that would have worked.
    """
    u = (url or "").strip().rstrip(".,);]'\"").lower()
    u = re.sub(r"^https?://", "", u)
    u = u.split("#", 1)[0]
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


# The three shapes a page view uses to say where it is. All three,
# because item_state shipped reading only two of them and the guard it
# was built for never fired once in four live runs: browser_extract
# wraps its output with wrap_untrusted, whose header is "Source:".
_PAGE_URL_RES = (
    re.compile(r"^URL:\s*(\S+)", re.MULTILINE),
    re.compile(r"Now on:.*?\((\S+?)\)"),
    re.compile(r"^Source:\s*(\S+)", re.MULTILINE),
    # "[page text — <url>]", "[records — <url>]", "[outline — <url>]".
    # The fourth shape, found by replaying a real trace after the other
    # three had already been called complete once.
    re.compile(r"^\[[^\]\n]*?(https?://[^\s\]]+)\]", re.MULTILINE),
)

# The arguments that carry a DESCRIPTION of what is wanted, in the order
# the tools use them. An attempt with none of these has no intent to
# remember and is left alone.
_QUERY_ARGS = ("query", "description", "target", "heading", "text",
               "section", "label")


def page_in(view: str) -> str:
    """The URL a page view reports it is on, or ""."""
    for rx in _PAGE_URL_RES:
        m = rx.search(view or "")
        if m:
            return m.group(1)
    return ""


def query_in(args: Dict[str, object]) -> str:
    """What this call says it is looking for, or ""."""
    for name in _QUERY_ARGS:
        val = (args or {}).get(name)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _stem(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and not word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    # "monthly change column" and "1 month performance column" are the
    # same column asked for twice. Without this they share one word out
    # of three and read as different questions.
    if len(word) > 4 and word.endswith("ly"):
        return word[:-2]
    return word


def subject_words(text: str) -> Set[str]:
    """What is being asked for, with the phrasing taken out."""
    raw = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {_stem(w) for w in raw if w and w not in _STOP and len(w) > 1}


def same_intent(a: Set[str], b: Set[str]) -> bool:
    if not a or not b:
        return False
    shared = len(a & b)
    if not shared:
        return False
    return shared / min(len(a), len(b)) >= SAME_INTENT


def _view_hash(view: str) -> str:
    text = (view or "").strip()
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class _Attempt:
    words: Set[str] = field(default_factory=set)
    query: str = ""
    tool: str = ""
    view: str = ""


class StrategyMemory:
    """Per-run, in memory. A page's layout is a fact about right now."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (page, family) -> attempts that came back with nothing
        self._failed: Dict[Tuple[str, str], List[_Attempt]] = {}
        # (page, family) -> a description that DID return something, so
        # the note can point somewhere instead of only saying no.
        self._worked: Dict[Tuple[str, str], str] = {}

    def _key(self, page: str, tool: str) -> Optional[Tuple[str, str]]:
        fam = family_of(tool)
        if not fam:
            return None
        if fam == SEARCH:
            # One scope for the whole web. See SEARCH_SCOPE.
            return (SEARCH_SCOPE, SEARCH)
        page_n = _norm_page(page)
        if not page_n:
            return None
        return (page_n, fam)

    def record(self, tool: str, page_url: str, query: str, result: str,
               page_view: str = "") -> None:
        """File what this attempt actually returned. Never raises."""
        key = self._key(page_url, tool)
        if key is None:
            return
        words = subject_words(query)
        if not words:
            return
        # A search is remembered whether or not it returned rows -- the
        # corpus does not change between two identical questions. Every
        # other family remembers only what came back empty.
        repeat_worthy = (key[1] == SEARCH) or looks_fruitless(result, tool)
        with self._lock:
            if repeat_worthy:
                self._failed.setdefault(key, []).append(
                    _Attempt(words=words, query=(query or "")[:80],
                             tool=str(tool), view=_view_hash(page_view)))
            elif result:
                self._worked.setdefault(key, (query or "")[:80])

    def _matches(self, tool: str, page_url: str, query: str):
        key = self._key(page_url, tool)
        if key is None:
            return [], None
        words = subject_words(query)
        if not words:
            return [], key
        with self._lock:
            prior = list(self._failed.get(key, ()))
        return [a for a in prior if same_intent(words, a.words)], key

    def _fruitless_on_page(self, tool: str, page_url: str) -> List[_Attempt]:
        key = self._key(page_url, tool)
        if key is None:
            return []
        with self._lock:
            return list(self._failed.get(key, ()))

    def note_for(self, tool: str, page_url: str,
                 query: str) -> Optional[str]:
        """What to tell the model before it asks the same question again."""
        hits, key = self._matches(tool, page_url, query)
        if key is not None and key[1] == SEARCH:
            return self._search_note(hits)
        if len(hits) < NOTE_AFTER:
            return self._page_note(tool, page_url, key)
        tried = ", ".join('"' + a.query + '"' for a in hits[-3:])
        note = (
            f"(NOTE BEFORE THIS CALL: you have already looked for this on "
            f"this page {len(hits)} time(s) and been told there is nothing "
            f"matching - {tried}. Those searches covered every element on "
            f"the page, so rewording is not the fix. "
        )
        with self._lock:
            worked = self._worked.get(key) if key else None
        if worked:
            note += ('"' + worked + '" DID return something here - build on '
                     "that, or ")
        else:
            note += "Either "
        return note + (
            "change the page itself: open a different tab, view or preset, "
            "scroll, or put the setting in the URL. If none of those exist, "
            "the page cannot do this - read what it DOES show and say so.)"
        )

    def _search_note(self, hits: List[_Attempt]) -> Optional[str]:
        """What to say before asking the web the same question again.

        Different from every other note here, because the answer is not
        about a page. A search engine given the same question returns
        the same corpus; the way forward is to OPEN one of the results
        already in hand, not to word the question differently.
        """
        if len(hits) < SEARCH_NOTE_AFTER:
            return None
        tried = "; ".join('"' + a.query + '"' for a in hits[-4:])
        return (
            f"(NOTE BEFORE THIS CALL: you have already searched the web "
            f"{len(hits)} time(s) for this same thing — {tried}. The web has "
            f"not changed since, so rewording the query returns the same "
            f"corpus. If earlier results named a promising page, OPEN it with "
            f"action.browser_navigate or action.web_read and read what it "
            f"says. Searching again is not progress.)"
        )

    def _page_note(self, tool: str, page_url: str,
                   key) -> Optional[str]:
        """The coarser note: this page keeps saying there is nothing here."""
        prior = self._fruitless_on_page(tool, page_url)
        if len(prior) < PAGE_NOTE_AFTER:
            return None
        tried = ", ".join('"' + a.query + '"' for a in prior[-4:])
        with self._lock:
            worked = self._worked.get(key) if key else None
        note = (
            f"(NOTE BEFORE THIS CALL: this page has been searched "
            f"{len(prior)} times this run and answered NOTHING FOUND every "
            f"time - {tried}. Each of those searched every element on the "
            f"page. "
        )
        if worked:
            note += ('"' + worked + '" is the only thing that has returned '
                     "anything here. ")
        return note + (
            "Before searching again, change what is on the screen - a "
            "different tab, view or preset, a scroll, or the setting in the "
            "URL - or accept that this page does not carry what you want and "
            "read what it does show.)"
        )

    def refusal_for(self, tool: str, page_url: str, query: str,
                    page_view: str = "") -> Optional[str]:
        """A hard stop, on either rule, and only on an unchanged page.

        The page-unchanged test is what keeps this honest. A tab that
        opened, a menu that expanded or a scroll that loaded more rows
        makes the same words a different question, and that attempt is
        let through with a note instead.
        """
        hits, key = self._matches(tool, page_url, query)

        # A SEARCH HAS NO PAGE TO HAVE CHANGED, so the unchanged-page
        # test below cannot gate it. The threshold carries the weight
        # instead: a fifth identical question to a search engine cannot
        # return what the first four did not, and by then the run has
        # results in hand it has not opened.
        if key is not None and key[1] == SEARCH:
            if len(hits) < SEARCH_REFUSE_AFTER:
                return None
            return (
                f"(REFUSED: this run has already searched the web "
                f"{len(hits)} times for the same thing. The corpus has not "
                f"changed between those queries, so a differently-worded "
                f"version of the same question cannot return anything new. "
                f"OPEN one of the pages the earlier searches already named "
                f"and read it, or report honestly that no source carrying "
                f"this was found.)"
            )

        prior = self._fruitless_on_page(tool, page_url)
        same_q = len(hits) >= REFUSE_AFTER
        tired_page = len(prior) >= PAGE_REFUSE_AFTER
        if not same_q and not tired_page:
            return None

        # Unchanged since the last attempt that earned the refusal --
        # the same-question one if that is what fired, otherwise the
        # most recent fruitless search of this page.
        last = hits[-1] if same_q else prior[-1]
        now, before = _view_hash(page_view), last.view
        if not now or not before or now != before:
            return None

        if same_q:
            what = (f"already searched this page for the same thing "
                    f"{len(hits)} times")
        else:
            what = (f"searched this page {len(prior)} times and been told "
                    f"every time that there is nothing matching")
        return (
            f"(REFUSED: this run has {what}, and the page has not changed "
            f"since the last one - same bytes, same elements. Those searches "
            f"covered every element on the page. Asking again in different "
            f"words cannot return a different answer. Do something that "
            f"changes what is on the screen - a different view, tab, preset, "
            f"scroll or URL - or work with what this page does show and "
            f"report that.)"
        )


_memory: Optional[StrategyMemory] = None
_memory_lock = threading.Lock()


def get_memory() -> StrategyMemory:
    global _memory
    with _memory_lock:
        if _memory is None:
            _memory = StrategyMemory()
        return _memory


def reset() -> None:
    """A new run starts with no verdicts. Tests, and the loop's start."""
    global _memory
    with _memory_lock:
        _memory = StrategyMemory()
