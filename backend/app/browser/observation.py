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

# How many iframes are observed. Ad-heavy pages carry dozens of tiny
# tracking frames; the ones that hold real controls are few and near the
# front. A cap keeps an observation from being buried in advertising.
MAX_FRAMES = int(__import__("os").environ.get("BROWSER_MAX_FRAMES", "8"))

# "f2e7" — the seventh element of the second frame.
_FRAME_ID = re.compile(r"^f(\d+)(e\d+)$")

# The data fingerprint, as a self-contained JS function.
#
# ONE definition, used two ways: interpolated into _OBSERVE_JS so an
# observation computes it in the same DOM read as everything else, and
# exposed as _DATA_JS so the four tools that build their own output
# (navigate, extract, extract_table, click) can carry it too. Copying it
# would have been simpler and is exactly the "one list living in two
# files and drifting apart" that this codebase keeps getting bitten by.
_DATA_FN_JS = r"""
(() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  // Strip the separator the fingerprint line uses: a key containing it
  // would split into two on the way back and make an untouched page look
  // like it had changed. Collapse whitespace too — real headers carry
  // non-breaking spaces ("Chg %"), and a column name differing only by
  // an invisible character reads as a DIFFERENT column.
  const key = (s) => (s || '').replace(/·/g, ' ').replace(/\s+/g, ' ')
                              .trim().slice(0, 18);

  // Real cells this has to survive, from a live extraction:
  //   "225.16 USD"  "−0.06%"  "75.68 M"  "5.45 T USD"  "1,180.16 USD"
  //   "—"  ""  "+697.29%"  "755,570.01 USD"
  // The minus is U+2212, not a hyphen; missing values are an em dash.
  const MULT = { k: 1e3, m: 1e6, b: 1e9, t: 1e12 };
  const num = (s) => {
    if (!s) return null;
    const t = String(s).replace(/−/g, '-').replace(/,/g, '').trim();
    const m = t.match(/^[^\d\-+.]*([-+]?\d*\.?\d+)\s*([kmbt])?\b/i);
    if (!m) return null;
    let v = parseFloat(m[1]);
    if (!isFinite(v)) return null;
    if (m[2]) v *= MULT[m[2].toLowerCase()];
    return v;
  };

  // A POSITION, not a measurement. A rank or row-number column reads
  // 1,2,3... whatever order the rows are in — so it is invariant under
  // sorting, which makes it both a useless row key (the fingerprint
  // never changes) and a fake sort signal (it is always "ascending",
  // even on a table nobody has touched). Measured live: a screener with
  // a "#" column reported rows_changed=False across a genuine re-sort
  // AND SORTED: # ascending on the untouched default view.
  const isOrdinal = (cells) => {
    const ns = cells.map(num);
    if (ns.length < 3 || ns.some(v => v === null || !Number.isInteger(v))) return false;
    const s = ns.slice().sort((a, b) => a - b);
    for (let i = 1; i < s.length; i++) if (s[i] !== s[i - 1] + 1) return false;
    return true;
  };

  // THE BIGGEST TABLE IS NOT ALWAYS THE DATA.
  //
  // Finviz's screener puts its FILTER PANEL in a table with 55 rows and
  // the results in one with 21, so "most cells" picked the controls and
  // measured the sort on a page of dropdowns. A data table is
  // distinguished by its CONTENT: many of its cells parse as numbers,
  // and every row is the same width. A control panel is neither.
  let best = null, bestScore = 0;
  document.querySelectorAll('table').forEach((tbl) => {
    const trs = Array.from(tbl.querySelectorAll('tr'));
    if (trs.length < 3) return;                 // layout markup, not data
    const sample = trs.slice(0, 12).map(
      tr => Array.from(tr.querySelectorAll('th,td')).map(c => clean(c.innerText)));
    const widths = sample.map(r => r.length).filter(w => w > 0);
    if (widths.length < 3) return;
    const flat = sample.flat();
    if (!flat.length) return;
    const numeric = flat.filter(c => num(c) !== null).length / flat.length;
    // Consistent row width: a real table is rectangular, a layout table
    // is whatever fitted.
    const modal = widths.sort((a, b) =>
      widths.filter(w => w === a).length - widths.filter(w => w === b).length).pop();
    const regular = widths.filter(w => w === modal).length / widths.length;
    if (numeric < 0.15 || regular < 0.6) return;
    const score = trs.length * (0.5 + numeric) * regular;
    if (score > bestScore) { bestScore = score; best = trs; }
  });
  if (!best) return null;

  // Drop a pure header row — constant, so it would spend a slot and
  // never contribute to change detection.
  const body = best.filter(tr => tr.querySelector('td'));
  const use = body.length ? body : best;
  const rows = use.slice(0, 14).map(
    tr => Array.from(tr.querySelectorAll('th,td')).map(c => clean(c.innerText))
  );
  if (!rows.length) return null;

  const width = Math.max.apply(null, rows.map(r => r.length).concat([0]));
  const column = (c) => rows.map(r => r[c] || '');

  // CHOOSE THE KEY COLUMN by scanning left to right for the first one
  // that actually identifies a row. The old rule took column 0 unless it
  // failed a DISTINCTNESS test — and a rank column is maximally
  // distinct, so it was never rejected.
  let keyCol = -1;
  for (let c = 0; c < width; c++) {
    const cells = column(c);
    const filled = cells.filter(Boolean);
    if (filled.length < rows.length * 0.8) continue;   // checkbox / icon column
    if (new Set(filled.map(key)).size < Math.max(2, Math.ceil(rows.length * 0.6))) continue;
    if (isOrdinal(cells)) continue;                     // a position, not an identity
    keyCol = c;
    break;
  }
  // No column identifies a row — say UNKNOWN rather than invent a
  // positional key, which would be constant and therefore a guard that
  // can never fail.
  if (keyCol < 0) return null;

  const keys = rows.map(r => key(r[keyCol]));

  // IS ANY COLUMN ACTUALLY IN ORDER? Measured from the numbers on the
  // page, never inferred from what the agent believes it did. A click
  // that opened a filter and a URL parameter the site ignored both leave
  // every column unsorted, and both previously read as success.
  const header = best.find(tr => tr.querySelector('th'));
  const names = header
    ? Array.from(header.querySelectorAll('th,td')).map(c => key(c.innerText))
    : [];
  const sorted = [];
  for (let c = 0; c < width; c++) {
    if (c === keyCol) continue;
    const cells = column(c);
    if (isOrdinal(cells)) continue;      // same rule, one definition
    const known = cells.map(num).filter(v => v !== null);
    // Enough of the column must be numeric to call it a number column,
    // and enough rows must exist for "in order" to mean anything —
    // three rows ascending is a coincidence.
    if (known.length < 4 || known.length < rows.length * 0.8) continue;
    if (new Set(known).size < 2) continue;            // all equal is not sorted
    let asc = true, desc = true;
    for (let i = 1; i < known.length; i++) {
      if (known[i] > known[i - 1]) desc = false;
      if (known[i] < known[i - 1]) asc = false;
    }
    if (asc === desc) continue;                        // neither, or constant
    sorted.push({ column: names[c] || ('column ' + (c + 1)),
                  direction: desc ? 'descending' : 'ascending' });
  }

  return { keys: keys, total: use.length, sorted: sorted };
})
"""

# Standalone form, for the tools that build their own output.
_DATA_JS = "() => (" + _DATA_FN_JS + ")()"


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

  // THE DATA FINGERPRINT — the answer to "did the ROWS move", which is a
  // different question from "did the page change" and the only one that
  // matters for a task about data.
  //
  // Row KEYS, not cell values, deliberately. A screener's prices tick
  // every second, so comparing values would report "changed" on every
  // single observation and quietly disable every check built on it. A
  // ticker does not move unless something sorted or filtered the table.
  const rowKeys = __DATA_FN__;

  let data = null;
  try { data = rowKeys(); } catch (e) { data = null; }

  return {
    gen: GEN,
    url: window.location.href,
    title: document.title || '',
    text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n'),
    elements,
    truncated_elements: document.querySelectorAll(SEL).length > LIMIT,
    data_keys: data ? data.keys : null,
    data_total: data ? data.total : 0,
    sorted_columns: data ? data.sorted : null,
  };
}
""".replace("__DATA_FN__", _DATA_FN_JS)


class Observation:
    """One snapshot of a page, and the generation its ids belong to."""

    __slots__ = ("gen", "url", "title", "text", "elements", "truncated",
                 "data_keys", "data_total", "sorted_columns")

    def __init__(self, raw: Dict[str, Any], gen: int) -> None:
        self.gen = gen
        self.url = str(raw.get("url") or "")
        self.title = str(raw.get("title") or "")
        self.text = str(raw.get("text") or "")[:MAX_TEXT_CHARS]
        self.elements: List[Dict[str, Any]] = list(raw.get("elements") or [])
        self.truncated = bool(raw.get("truncated_elements"))
        # None means "this page has no data table", which is a different
        # state from "it has one and it is empty". Callers must be able to
        # tell them apart -- see rows_changed, which answers UNKNOWN rather
        # than guessing.
        keys = raw.get("data_keys")
        self.data_keys: Optional[List[str]] = (
            [str(k) for k in keys] if isinstance(keys, list) else None)
        self.data_total = int(raw.get("data_total") or 0)
        cols = raw.get("sorted_columns")
        self.sorted_columns: List[Dict[str, str]] = (
            [c for c in cols if isinstance(c, dict)] if isinstance(cols, list) else [])

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
        # THE FINGERPRINT GOES THIRD, before the element list.
        #
        # It was last, and on a dense page that put it out of reach of
        # every cap that has to see it. Measured on a 120-element page it
        # landed at character 7068, while the model is only ever shown
        # the first MAX_OBSERVATION_CHARS (1500) of a tool result. So the
        # loop could tell a model "the rows did not move" while the
        # evidence for it was truncated away -- and the check would have
        # worked on sparse pages and silently stopped on dense ones,
        # which is the worst possible failure profile given that dense
        # pages are the reason this layer exists.
        #
        # At offset ~60 it clears every cap at once and needs none raised.
        lines = [f"URL: {self.url}", f"TITLE: {self.title}",
                 self.data_line(), "", "INTERACTIVE ELEMENTS:"]
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

    def data_line(self) -> str:
        """The fingerprint, as one line the model reads and the loop parses.

        It goes in the TOOL'S TEXT OUTPUT rather than travelling as a
        separate structured field, and that is the load-bearing choice:
        the ledger already records tool output whole, and the loop already
        compares it. One definition of "the data changed", shared by the
        loop and the gate, instead of two that can drift apart.
        """
        if self.data_keys is None:
            return f"{DATA_PREFIX} {_NO_TABLE}"
        if not self.data_keys:
            return f"{DATA_PREFIX} {_EMPTY_TABLE}"
        shown = " · ".join(self.data_keys)
        if self.sorted_columns:
            order = "; ".join(f"{c.get('column')} {c.get('direction')}"
                              for c in self.sorted_columns)
        else:
            order = _NO_SORT
        return (f"{DATA_PREFIX} {self.data_total} row(s) | {shown}\n"
                f"{SORTED_PREFIX} {order}")


DATA_PREFIX = "DATA:"
_NO_TABLE = "(no data table on this page)"
_EMPTY_TABLE = "(table present but empty)"
_DATA_LINE = re.compile(
    rf"^{re.escape(DATA_PREFIX)}\s*(?:(\d+) row\(s\) \| )?(.*)$", re.MULTILINE)


def parse_data_keys(text: str) -> Optional[List[str]]:
    """The row keys recorded in a tool result, or None if there were none.

    None means UNKNOWN -- either the output predates the fingerprint, or
    the page had no table. It must never be read as "the rows did not
    change": a check that treats missing evidence as passing evidence is
    the shape of every guard this codebase has had to rewrite.
    """
    m = _DATA_LINE.search(text or "")
    if not m:
        return None
    body = (m.group(2) or "").strip()
    if not body or body in (_NO_TABLE, _EMPTY_TABLE):
        return [] if body == _EMPTY_TABLE else None
    return [k.strip() for k in body.split("·") if k.strip()]


def rows_changed(before: str, after: str) -> Optional[bool]:
    """Did the DATA move between two tool results?

    Three-valued on purpose. None means "cannot tell" -- no fingerprint on
    one side, or no table at all -- and every caller has to handle it
    explicitly rather than collapsing it into True or False. The
    difference between "the rows did not move" and "there were no rows to
    move" is exactly the difference between a failed action and an
    inapplicable check.
    """
    a, b = parse_data_keys(before), parse_data_keys(after)
    if a is None or b is None:
        return None
    return a != b


SORTED_PREFIX = "SORTED:"
_NO_SORT = "(no column is in order)"
_SORTED_LINE = re.compile(rf"^{re.escape(SORTED_PREFIX)}\s*(.*)$", re.MULTILINE)


def parse_sorted_columns(text: str) -> Optional[List[Dict[str, str]]]:
    """Which columns a recorded page reported as being in order.

    None when the output carries no SORTED line at all -- unknown, not
    "nothing is sorted". [] means the page was checked and no column was
    monotonic, which is a real and useful answer.

    WHY THIS IS MEASURED AT THE PAGE rather than inferred from row keys.
    The obvious test -- "did the same rows come back in a different
    order?" -- fails on exactly the tables that matter. Measured live on
    TradingView: sorting a 100-row screener whose fingerprint samples the
    top 14 replaces every sampled row, so a genuine sort and a filter look
    identical. Monotonicity of the numbers is what a sort actually means.
    """
    m = _SORTED_LINE.search(text or "")
    if not m:
        return None
    body = (m.group(1) or "").strip()
    if not body or body == _NO_SORT:
        return []
    out: List[Dict[str, str]] = []
    for part in body.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, direction = part.rpartition(" ")
        if name and direction in ("ascending", "descending"):
            out.append({"column": name.strip(), "direction": direction})
    return out


def fingerprint_lines(page) -> str:
    """The DATA/SORTED lines for a page, without a full observation.

    For the four tools that build their own output and never call
    observe_page: browser_navigate, browser_extract, browser_extract_table
    and browser_click. Every one of them is a page-view tool by the loop's
    own predicates, and every one was carrying no fingerprint at all --
    which mattered most for browser_navigate, since navigating to a sort
    parameter the site ignores is the exact failure the fingerprint exists
    to catch, and it happens entirely inside that tool.

    MUST be called on the browser thread. Never raises: a page that
    cannot be measured yields no line, and no line means UNKNOWN.
    """
    try:
        raw = page.evaluate(_DATA_JS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fingerprint unavailable: %s", exc)
        return ""
    raw = raw or {}
    return Observation({"data_keys": raw.get("keys"),
                        "data_total": raw.get("total") or 0,
                        "sorted_columns": raw.get("sorted")}, 0).data_line()


def is_sorted_somehow(text: str) -> bool:
    """True when the page reported at least one column genuinely in order.

    Kept as a named predicate precisely BECAUSE it is not the right test
    on its own: it returns True on TradingView's default view, which
    arrives sorted by market cap. Use sort_changed to decide whether the
    agent produced a ranking. This one only answers "is this table in
    some order at all".
    """
    return bool(parse_sorted_columns(text))


def _sort_signature(text: str) -> Optional[frozenset]:
    cols = parse_sorted_columns(text)
    if cols is None:
        return None
    return frozenset((c["column"], c["direction"]) for c in cols)


def sort_changed(before: str, after: str) -> Optional[bool]:
    """Did THIS RUN change how the table is ordered?

    THE CORRECTION THAT MAKES THIS CHECK WORTH ANYTHING. The obvious test
    -- "is some column in order?" -- was measured live and returns TRUE ON
    THE DEFAULT VIEW: TradingView's screener arrives sorted by market cap
    descending. So "something is sorted" would have passed the exact run
    this exists to reject, where the agent read the default list and
    called it the top weekly gainers.

    A page arriving sorted is not the agent's doing. What proves the agent
    produced a ranking is that the ordering it is reading is DIFFERENT
    from the one the page handed it.

    Verified end to end on the live screener: "Mkt cap descending" ->
    "Chg % descending", rows NVDA/AAPL/GOOG -> TREVQ/ETBI/IOBTQ.

    None when either side carries no measurement -- unknown, never
    silently "no".
    """
    a, b = _sort_signature(before), _sort_signature(after)
    if a is None or b is None:
        return None
    return a != b


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
        # Frame index -> the Playwright Frame it came from. Cleared each
        # generation, because a frame from a previous observation may be
        # detached and resolving into it would be the stale-reference bug
        # this whole id scheme exists to prevent.
        self._frames: Dict[int, Any] = {}

    @property
    def generation(self) -> int:
        return self._gen

    @property
    def last(self) -> Optional[Observation]:
        return self._last

    def new_generation(self) -> int:
        self._gen += 1
        self._frames = {}
        return self._gen

    def record(self, obs: Observation) -> None:
        self._last = obs

    def record_frame(self, index: int, frame: Any) -> None:
        self._frames[index] = frame

    def frame_for(self, element_id: str) -> Optional[Any]:
        """The frame an id lives in, or None for the main document.

        `f2e7` means the seventh element of the second frame. The prefix
        is part of the id rather than a separate argument so a model
        cannot address an element in one frame while naming another.
        """
        m = _FRAME_ID.match(element_id or "")
        if not m:
            return None
        return self._frames.get(int(m.group(1)))

    def selector_for(self, element_id: str) -> str:
        # The stamp inside a frame is the UNPREFIXED id — the prefix
        # identifies the document, not the element within it.
        m = _FRAME_ID.match(element_id or "")
        bare = m.group(2) if m else element_id
        return f'[data-vai-id="{bare}"]'

    def check(self, element_id: str) -> Optional[str]:
        """None when the id is usable, else why it is not.

        Refusing an id we have never seen is as important as refusing a
        stale one: a model that invents `e99` should be corrected, not
        silently pointed at nothing.
        """
        if not self._last:
            return ("no observation yet — call browser_observe first to see "
                    "what is on the page")
        if not element_id or not (element_id.startswith("e")
                                  or _FRAME_ID.match(element_id)):
            return (f"{element_id!r} is not an element id. Ids look like 'e17', "
                    "or 'f1e3' for something inside a frame, and come from "
                    "browser_observe.")
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

    # IFRAMES ARE PART OF THE PAGE, and were invisible.
    #
    # page.evaluate runs in the main document only, so an embedded form,
    # a chat widget, a checkout step or a consent wall simply did not
    # exist as far as the model was concerned -- it saw an empty region
    # where the controls were and had no way to know why. Measured on
    # theguardian.com, whose entire consent dialog lives in a
    # cross-origin frame.
    #
    # Frame elements get an id prefixed with the frame index (f1e3), and
    # ElementMap remembers which frame each id belongs to so a click
    # resolves inside the right document. Same stamped-attribute
    # mechanism, one document deeper.
    frame_index = 0
    for frame in _child_frames(page):
        if len(obs.elements) >= limit:
            break
        frame_index += 1
        prefix = f"f{frame_index}"
        try:
            fraw = frame.evaluate(
                _OBSERVE_JS,
                {"gen": gen, "limit": max(1, int(limit) - len(obs.elements))},
            ) or {}
        except Exception:  # noqa: BLE001
            # A frame that detached mid-observation, or one that refuses
            # script access, is skipped rather than failing the whole
            # observation.
            continue
        for el in (fraw.get("elements") or []):
            el = dict(el)
            el["id"] = f"{prefix}{el.get('id')}"
            el["frame"] = frame_index
            obs.elements.append(el)
        element_map.record_frame(frame_index, frame)

    element_map.record(obs)

    # LINKS THE PAGE SHOWED ARE "SEEN", NOT INVENTED.
    #
    # The source ledger has always had two states -- fetched, and
    # surfaced-by-a-search-but-never-opened -- and nothing recorded the
    # second for a link read off a page. So an agent that read a results
    # page, saw each listing's href, and cited one was reported as
    # fabricating it. That is the same false accusation the trailing
    # backtick caused, and it blocks honest work.
    #
    # A citation to a link the page displayed is verifiable and true. A
    # citation to a URL that appeared nowhere still is not.
    try:
        from backend.app.tools.source_ledger import get_ledger
        ledger = get_ledger()
        for el in obs.elements:
            href = el.get("href")
            if href:
                ledger.record_seen(str(href))
    except Exception:  # noqa: BLE001
        pass

    logger.info(
        "observed %s — %d interactive element(s) across %d frame(s), gen %d",
        obs.url[:80], len(obs.elements), frame_index + 1, gen,
    )
    return obs


def _child_frames(page):
    """Frames worth observing: same-page, not the main document, and not
    the empty placeholders a page leaves behind."""
    out = []
    try:
        for f in page.frames:
            if f is page.main_frame:
                continue
            url = (f.url or "")
            if not url or url == "about:blank":
                continue
            out.append(f)
    except Exception:  # noqa: BLE001
        return []
    return out[:MAX_FRAMES]
