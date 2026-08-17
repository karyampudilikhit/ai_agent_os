"""Semantic page observation — what the model sees instead of pixels.

THE PROBLEM THIS REPLACES. The existing browser tools address elements by
their VISIBLE TEXT: `browser_click(target="Deploy")`. That works until a
page has two buttons reading "Deploy", or a control that is an icon with
no text at all, or a label that changes between renders. It also cannot
express "the third row's checkbox" or "the empty project-name field",
which is most of what operating a real web app consists of.

So an observation assigns every interactive element a SHORT ID and hands
the model a structured list:

    {"id": "e17", "role": "button",  "name": "Deploy", "enabled": true}
    {"id": "e18", "role": "textbox", "name": "Project name", "value": ""}

and the model replies `{"action": "click", "element_id": "e17"}`.

HOW e17 RESOLVES BACK TO AN ELEMENT. Observation stamps a `data-vai-id`
attribute onto each element it lists, and the controller later locates by
that attribute. Chosen over the alternatives on purpose:

  - an index into a saved list breaks the moment anything re-renders,
    and silently addresses the WRONG element rather than failing
  - an XPath breaks on any structural change and is unreadable in logs
  - a stamped attribute either survives (and is exact) or is gone (and
    fails loudly, which is what a stale reference should do)

IDs ARE PER-OBSERVATION AND DELIBERATELY NOT STABLE. e17 means nothing
after a navigation or a re-render. `observe()` bumps a generation counter
and stamps fresh ids; resolving an id from an older generation is
refused with a message telling the model to observe again. A stale id
that quietly resolved to whatever now sits in that position is the exact
failure this design exists to prevent.

TRUST BOUNDARY. Everything in an observation is UNTRUSTED. Page text is
data the model reads, never instruction it follows -- see
browser_policy.wrap_untrusted, which every text-returning browser tool
routes through. A page saying "ignore your previous instructions" is
content, and is presented as content.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Cap on how many elements one observation reports. A dense app can carry
# hundreds of focusable nodes; dumping all of them buries the useful ones
# and burns the step's token budget on navigation chrome. Interactive
# elements are emitted in document order, so the cap keeps the top of the
# page -- which is where the primary action almost always is.
MAX_ELEMENTS = int(__import__("os").environ.get("BROWSER_MAX_ELEMENTS", "120"))
MAX_TEXT_CHARS = int(__import__("os").environ.get("BROWSER_MAX_TEXT_CHARS", "3000"))

# How deep browser_find scans before filtering. Much larger than
# MAX_ELEMENTS on purpose: the cap above exists so an OBSERVATION stays
# readable, and a search has no such constraint -- it returns a handful
# of matches whether it considered 120 candidates or 600.
#
# This is the difference that matters on a dense app. TradingView's
# screener carries several hundred controls and its column headers sit
# below the cap, so every observation showed the model navigation chrome
# and no way to sort. The control was not hard to choose; it was never
# on the list.
FIND_SCAN_LIMIT = int(__import__("os").environ.get("BROWSER_FIND_SCAN_LIMIT", "600"))
FIND_RESULTS = int(__import__("os").environ.get("BROWSER_FIND_RESULTS", "12"))

_OBSERVE_JS = r"""
(args) => {
  const GEN = args.gen;
  const LIMIT = args.limit;
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 160);

  // A control inside a table cell takes its meaning from the CELL.
  //
  // This is the fix for the failure that stopped six TradingView runs.
  // Its sort controls are icon-only buttons inside each <th>: the arrow
  // is the button, and the word "Chg %" lives in the header cell around
  // it. Every one of them carries the same aria-label, so an observation
  // listed twelve buttons all named "Sort descending" and the model had
  // no way to say which column it was about to sort. It clicked the
  // toolbar's FILTER buttons instead -- those have distinct names -- and
  // the table never moved.
  //
  // Verified against the live page: this turns those twelve into
  // "Price — Sort descending", "Chg % — Sort descending", and so on.
  // General, not site-specific: an icon-only control in a labelled cell
  // is one of the most common patterns on the web, and nameOf already
  // walks <label> ancestors for exactly this reason -- table cells were
  // simply missing from the list.
  // A control's own label is often a generic verb -- "Sort descending",
  // "Change sort", "Options". Appending those to the column name puts
  // ranking words into the identity of the WRONG column: a search for
  // "Change %" ranked "Mkt cap — Change sort" above "Chg %", because the
  // literal word "change" was sitting in a suffix that says nothing
  // about which column it is. The cell's own label IS the identity.
  const GENERIC_ACTION = /^(sort|change sort|sort (ascending|descending)|(ascending|descending)|menu|options|more|settings|actions?)$/i;
  const cellLabel = (el, own) => {
    const cell = el.closest('th, td');
    if (!cell) return own;
    const label = clean(cell.innerText);
    if (!label || label === own) return own;
    if (!own || GENERIC_ACTION.test(own)) return label;
    return label + ' — ' + own;
  };

  // Accessible name, in roughly the order a screen reader resolves it.
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return cellLabel(el, clean(aria));
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const t = labelledby.split(/\s+/).map(id => {
        const n = document.getElementById(id);
        return n ? n.innerText : '';
      }).join(' ');
      if (clean(t)) return clean(t);
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && clean(lbl.innerText)) return clean(lbl.innerText);
    }
    const wrap = el.closest('label');
    if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    if (el.placeholder) return clean(el.placeholder);
    const title = el.getAttribute('title');
    if (title) return cellLabel(el, clean(title));
    const txt = clean(el.innerText || el.value || '');
    if (txt) return cellLabel(el, txt);
    if (el.name) return cellLabel(el, clean(el.name));
    const alt = el.querySelector && el.querySelector('img[alt]');
    if (alt) return cellLabel(el, clean(alt.getAttribute('alt')));
    return cellLabel(el, '');
  };

  const roleOf = (el) => {
    // A control in a column header IS a column header, whatever tag it
    // happens to use. Reported as such so a search for "the change
    // column" has a role to match on instead of competing against a
    // toolbar button with the same words in its label.
    if (el.closest('th')) return 'columnheader';
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.href ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'file') return 'file';
      if (t === 'password') return 'password';
      return 'textbox';
    }
    return 'generic';
  };

  // Visible means: has a box, is not display:none/visibility:hidden, and
  // is not fully transparent. Off-screen-but-scrollable still counts —
  // the model can scroll to it, and hiding it would make half of a long
  // form unreachable.
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (parseFloat(s.opacity || '1') === 0) return false;
    return true;
  };

  const SEL = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[role="combobox"]',
    '[contenteditable="true"]', '[onclick]',
    // Column headers. Sorting a table by clicking its header is one of
    // the most common interactions on the web and NONE of the selectors
    // above reach it: the header cell is a <th> with no role and no
    // inline handler, and the arrow button inside it is
    // visibility:hidden until hovered. Measured on TradingView's
    // screener -- 14 sort buttons, 3 visible, 13 header cells, all
    // visible, and zero of them matched. Six runs failed to sort a table
    // whose sort controls were never on the list.
    'th', '[role="columnheader"]',
  ].join(',');

  // Clear any stamps from a previous generation so a stale id cannot
  // resolve against a re-rendered page.
  document.querySelectorAll('[data-vai-id]').forEach(e => e.removeAttribute('data-vai-id'));

  const elements = [];
  let n = 0;
  const seen = new Set();
  document.querySelectorAll(SEL).forEach((el) => {
    if (elements.length >= LIMIT) return;
    if (seen.has(el)) return;
    if (!visible(el)) return;
    const type = (el.type || '').toLowerCase();
    if (type === 'hidden') return;
    seen.add(el);

    const id = 'e' + (++n);
    el.setAttribute('data-vai-id', id);

    const role = roleOf(el);
    const item = { id, role, name: nameOf(el) };

    if (el.disabled) item.enabled = false;
    if (role === 'textbox' || role === 'password') item.value = clean(el.value || '');
    if (role === 'checkbox' || role === 'radio' || role === 'switch') {
      item.checked = !!el.checked || el.getAttribute('aria-checked') === 'true';
    }
    if (role === 'combobox' && el.options) {
      item.options = Array.from(el.options).map(o => clean(o.textContent)).filter(Boolean).slice(0, 40);
      item.value = clean(el.value || '');
    }
    if (role === 'link' && el.href) item.href = String(el.href).slice(0, 300);
    if (el.required) item.required = true;
    // Unnamed generic nodes are noise — an [onclick] div with no
    // accessible name is not addressable by a model anyway.
    if (!item.name && role === 'generic') { el.removeAttribute('data-vai-id'); n--; return; }
    elements.push(item);
  });

  return {
    gen: GEN,
    url: window.location.href,
    title: document.title || '',
    text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n'),
    elements,
    truncated_elements: document.querySelectorAll(SEL).length > LIMIT,
  };
}
"""


class Observation:
    """One snapshot of a page, and the generation its ids belong to."""

    __slots__ = ("gen", "url", "title", "text", "elements", "truncated")

    def __init__(self, raw: Dict[str, Any], gen: int) -> None:
        self.gen = gen
        self.url = str(raw.get("url") or "")
        self.title = str(raw.get("title") or "")
        self.text = str(raw.get("text") or "")[:MAX_TEXT_CHARS]
        self.elements: List[Dict[str, Any]] = list(raw.get("elements") or [])
        self.truncated = bool(raw.get("truncated_elements"))

    def find(self, element_id: str) -> Optional[Dict[str, Any]]:
        for el in self.elements:
            if el.get("id") == element_id:
                return el
        return None

    def render(self, include_text: bool = True) -> str:
        """The observation as the model sees it.

        Compact on purpose — one line per element. A JSON dump of the same
        data costs roughly three times the tokens and reads no better at a
        glance, and the step budget is the scarce resource in this loop.
        """
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "", "INTERACTIVE ELEMENTS:"]
        if not self.elements:
            lines.append("  (none found — the page may still be loading; try browser_wait)")
        for el in self.elements:
            bits = [f"  {el['id']}  {el.get('role', '?')}"]
            name = el.get("name")
            bits.append(f'"{name}"' if name else "(unnamed)")
            if el.get("enabled") is False:
                bits.append("[disabled]")
            if el.get("required"):
                bits.append("[required]")
            if "value" in el:
                v = el["value"]
                bits.append(f"= {v!r}" if v else "= (empty)")
            if el.get("checked") is not None and "checked" in el:
                bits.append("[checked]" if el["checked"] else "[unchecked]")
            if el.get("options"):
                opts = ", ".join(el["options"][:8])
                bits.append(f"options: {opts}")
            lines.append(" ".join(bits))
        if self.truncated:
            lines.append(f"  … more elements exist than the {MAX_ELEMENTS} shown; "
                         "scroll or narrow the page to reach them")
        if include_text and self.text.strip():
            lines += ["", "PAGE TEXT:", self.text]
        return "\n".join(lines)


_STOPWORDS = frozenset({
    "the", "a", "an", "for", "to", "of", "on", "in", "that", "which", "is",
    "and", "or", "with", "by", "this", "it", "at", "as", "be", "find",
})

# What a founder-ish word means in accessibility-role terms. The model
# asks for "the sort dropdown" or "the change column"; the page reports
# roles like "combobox" and "columnheader". Without this the query and
# the page never share vocabulary and every search scores zero.
#
# ORDER IS THE WEIGHTING. The first role is what the word most likely
# means; the rest are fallbacks worth progressively less. Scoring them
# equally made "column header" fit a plain button exactly as well as an
# actual columnheader, so a "Change password" button outranked the
# "Change %" column on a query for the change column -- a wrong answer
# arrived at through a real match, which is the hardest kind to notice.
_ROLE_WORDS: Dict[str, Sequence[str]] = {
    "button": ("button", "tab", "menuitem"),
    "dropdown": ("combobox", "listbox", "menuitem"),
    "select": ("combobox", "listbox"),
    "menu": ("combobox", "menuitem", "listbox"),
    "field": ("textbox",),
    "input": ("textbox",),
    "search": ("textbox", "searchbox"),
    "box": ("textbox",),
    "checkbox": ("checkbox", "switch"),
    "toggle": ("switch", "checkbox"),
    "link": ("link",),
    "tab": ("tab",),
    "column": ("columnheader", "button", "cell"),
    "header": ("columnheader", "button"),
    "heading": ("columnheader", "heading"),
}


# Roles that say something about WHERE an element sits, not merely that
# it can be clicked. Preferred over a bare button when scores tie.
_SPECIFIC_ROLES = frozenset({"columnheader", "combobox", "listbox", "tab", "searchbox"})


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9%+]+", (text or "").lower())
            if t and t not in _STOPWORDS]


def _role_weights(q_tokens: Sequence[str]) -> Dict[str, float]:
    """Roles the query implies, best guess first, worth most first."""
    weights: Dict[str, float] = {}
    for t in q_tokens:
        for rank, role in enumerate(_ROLE_WORDS.get(t, ())):
            # 2.5, 1.25, 0.83 … — a decisive lead for the primary meaning
            # and a real but small credit for the fallbacks.
            weights[role] = max(weights.get(role, 0.0), 2.5 / (rank + 1))
    return weights


def _match_score(el: Dict[str, Any], q_tokens: Sequence[str],
                 q_roles: Dict[str, float]) -> float:
    """How well one element answers the query.

    Deterministic and cheap on purpose. A second model call to pick an
    element would put a guess between the model and the page, and the
    whole point of this layer is that what the model addresses is what
    the page actually contains.
    """
    name = str(el.get("name") or "").lower()
    role = str(el.get("role") or "").lower()
    opts = " ".join(str(o) for o in (el.get("options") or [])).lower()
    href = str(el.get("href") or "").lower()

    score = 0.0
    for t in q_tokens:
        if t in name:
            # A whole-word hit beats a substring: "change" should not be
            # outranked by "Exchange" on a page full of both.
            score += 4.0 if re.search(rf"\b{re.escape(t)}\b", name) else 2.5
        elif t == role:
            score += 1.5
        elif t in opts:
            score += 1.5
        elif t in href:
            score += 1.0
    score += q_roles.get(role, 0.0)

    # An element named almost exactly what was asked for beats one that
    # merely contains the word: "Change %" over "Change password alerts".
    name_tokens = _tokens(name)
    if name_tokens:
        covered = sum(1 for t in name_tokens if t in q_tokens)
        score += 1.5 * (covered / len(name_tokens))
    return score


def find_elements(obs: "Observation", query: str,
                  limit: int = FIND_RESULTS) -> List[Dict[str, Any]]:
    """The elements on `obs` that best answer `query`, best first."""
    q_tokens = _tokens(query)
    if not q_tokens:
        return []
    q_roles = _role_weights(q_tokens)

    scored = [(_match_score(el, q_tokens, q_roles), i, el)
              for i, el in enumerate(obs.elements)]

    def _key(entry):
        score, order, el = entry
        # Ties break toward the MORE SPECIFIC role before document order.
        # "Chg %" exists twice on a screener: a toolbar filter button and
        # the column header that sorts. They score identically on name,
        # and document order hands it to the filter -- which is what the
        # agent clicked, six runs running. A column header is the better
        # answer to a question about data whenever the two tie.
        specific = 0 if str(el.get("role") or "") in _SPECIFIC_ROLES else 1
        return (-score, specific, order)

    hits = sorted((s for s in scored if s[0] > 0), key=_key)
    return [el for _, _, el in hits[:limit]]


def render_matches(obs: "Observation", query: str,
                   matches: Sequence[Dict[str, Any]]) -> str:
    """Search results as the model sees them.

    Reports how many elements were CONSIDERED, not just how many matched.
    "searched 412 elements" is the line that tells a model its control
    genuinely is not on this page -- so it scrolls, or changes the URL,
    instead of clicking hopefully at whatever ranked first.
    """
    head = f'SEARCHED {len(obs.elements)} element(s) on {obs.url} for "{query}".'
    if not matches:
        # Ordered by what actually works, learned the hard way. The first
        # version of this message led with "try the URL", and on a page
        # whose sort lives entirely in JavaScript that sent the agent to a
        # query parameter the site ignored -- it loaded, the address bar
        # showed the sort, and the rows were the default ones. Changing
        # the VIEW is the move that finds a missing column; the URL is
        # worth trying only after that, and only if the address visibly
        # carries state.
        return "\n".join([
            head,
            "NO MATCH. Nothing on this page answers that description.",
            "",
            "Rewording is unlikely to help — this searched every element. "
            "The control is probably not on the CURRENT VIEW of the page. "
            "In order of what usually works:",
            "  1. Switch views. A column or metric you cannot see often "
            "lives under a different tab, mode or preset — look for tabs "
            "or a settings/columns control, open it, and search again.",
            "  2. Scroll. Lazy-loaded and off-screen regions render only "
            "once reached.",
            "  3. Only then, the URL — and only if the address above "
            "already carries this kind of state as a parameter. Inventing "
            "a parameter a site does not use loads a page that looks "
            "right and shows the default data, which is worse than "
            "failing.",
        ])

    lines = [head, f"BEST {len(matches)} MATCH(ES), most likely first:"]
    for el in matches:
        bits = [f"  {el['id']}  {el.get('role', '?')}"]
        name = el.get("name")
        bits.append(f'"{name}"' if name else "(unnamed)")
        if el.get("enabled") is False:
            bits.append("[disabled]")
        if "value" in el:
            bits.append(f"= {el['value']!r}" if el["value"] else "= (empty)")
        if el.get("checked") is not None and "checked" in el:
            bits.append("[checked]" if el["checked"] else "[unchecked]")
        if el.get("options"):
            bits.append("options: " + ", ".join(el["options"][:8]))
        if el.get("href"):
            bits.append(f"-> {el['href'][:120]}")
        lines.append(" ".join(bits))
    lines.append("")
    lines.append("These ids are live now. Use one with browser_click_element "
                 "or browser_select, then check that the page actually changed.")
    return "\n".join(lines)


class ElementMap:
    """Per-session generation counter + resolution of ids to locators.

    Holds no Playwright objects: resolution is a selector string built
    from the stamped attribute, so this class is safe to touch from any
    thread. Only the caller that runs the locator needs the browser
    thread.
    """

    def __init__(self) -> None:
        self._gen = 0
        self._last: Optional[Observation] = None

    @property
    def generation(self) -> int:
        return self._gen

    @property
    def last(self) -> Optional[Observation]:
        return self._last

    def new_generation(self) -> int:
        self._gen += 1
        return self._gen

    def record(self, obs: Observation) -> None:
        self._last = obs

    def selector_for(self, element_id: str) -> str:
        return f'[data-vai-id="{element_id}"]'

    def check(self, element_id: str) -> Optional[str]:
        """None when the id is usable, else why it is not.

        Refusing an id we have never seen is as important as refusing a
        stale one: a model that invents `e99` should be corrected, not
        silently pointed at nothing.
        """
        if not self._last:
            return ("no observation yet — call browser_observe first to see "
                    "what is on the page")
        if not element_id or not element_id.startswith("e"):
            return (f"{element_id!r} is not an element id. Ids look like 'e17' "
                    "and come from browser_observe.")
        if self._last.find(element_id) is None:
            known = ", ".join(e["id"] for e in self._last.elements[:12])
            return (f"{element_id} is not in the current observation. "
                    f"Visible ids: {known or '(none)'}. If the page changed, "
                    "call browser_observe again for fresh ids.")
        return None


def observe_page(page, element_map: ElementMap,
                 limit: int = MAX_ELEMENTS) -> Observation:
    """Snapshot `page`, stamp fresh ids, and record the observation.

    MUST be called on the browser thread — it touches the Page.

    `limit` raises how DEEP the scan goes, which is a different question
    from how much gets shown. Everything scanned is stamped and recorded,
    so a deep scan makes far-down elements addressable even when the
    caller only prints a handful of them — that is what lets
    browser_find reach a control that sits past the readable cap.
    """
    gen = element_map.new_generation()
    raw = page.evaluate(_OBSERVE_JS, {"gen": gen, "limit": max(1, int(limit))})
    obs = Observation(raw or {}, gen)
    element_map.record(obs)
    logger.info(
        "observed %s — %d interactive element(s), gen %d",
        obs.url[:80], len(obs.elements), gen,
    )
    return obs
