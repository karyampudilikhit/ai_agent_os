"""Semantic structure on an arbitrary page: sections, TOCs, lists, cards.

THE GAP THIS FILLS. The reading tools were whole-page text, tables, and
repeated records -- and nothing in between. Asked for "the History section
of the Artificial Intelligence article", a live run had the right page in
hand at step 2 and no instrument to slice it: browser_extract returns the
entire article, and a "#History" URL SCROLLS the browser without changing
a character of what gets read. Twenty calls, 675 seconds, bouncing
between the anchor and a site API, out of budget with the answer on
screen.

A second run, asked for a page's first five contents entries, escaped the
same way and succeeded only because that site happened to publish an API.
Most do not.

The pattern in both: when the tools cannot express what was asked, the
agent routes around the tools. That is not a model failure and no prompt
fixes it.

NOTHING HERE KNOWS WHAT SITE IT IS ON. Every rule below is a fact about
HTML, not about any publisher: headings nest by level, a table of
contents is a run of same-page fragment links, a card list is a set of
sibling elements with the same shape. A site-specific branch would fix
one page and teach us nothing about the next.

WHERE A SECTION ENDS. A heading owns everything until the next heading at
its own level or higher. With include_subsections the deeper headings
come too, because a subsection belongs to its parent; without it, the
first heading of ANY level ends the read. Both are legitimate questions
and the caller says which one they are asking.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec

logger = logging.getLogger(__name__)

MAX_SECTION_CHARS = 12000
MAX_STRUCTURE_CHARS = 12000
# Past this an "outline" is navigation furniture rather than structure.
MAX_OUTLINE_ITEMS = 200

# Shared by every query below: what counts as a heading. ARIA headings are
# included because component frameworks routinely style a <div> as one.
_HEADING_SEL = "h1,h2,h3,h4,h5,h6,[role=heading]"

_JS_HELPERS = r"""
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // Trailing "[edit]" / "edit" affordances are chrome, not the heading.
  const strip = (s) => clean(s).replace(/\s*\[\s*edit\s*\]\s*$/i, '').trim();
  const stripEl = (el) => strip(textOf(el));
  const HEADING_SEL = 'h1,h2,h3,h4,h5,h6,[role=heading]';
  const level = (el) => {
    const m = (el.tagName || '').match(/^H([1-6])$/);
    if (m) return Number(m[1]);
    const a = el.getAttribute && el.getAttribute('aria-level');
    const n = a ? Number(a) : NaN;
    return (n >= 1 && n <= 6) ? n : 2;
  };
  // TEXT THAT IS PRESENT BUT NOT RENDERED.
  //
  // innerText returns "" for anything inside a visibility:hidden or
  // collapsed container, while getBoundingClientRect still reports a
  // real box -- so a contents sidebar that is collapsed by default
  // passed the visibility check and then had EVERY entry dropped for
  // having no text. Measured live: the tool reported "0 entries, found
  // by: aria-label names a contents list", having located exactly the
  // right container.
  //
  // textContent is the fallback because it reads the DOM rather than
  // the render, which is precisely what is wanted for a list the page
  // will show the moment a reader expands it.
  const textOf = (el) => {
    const t = clean(el.innerText);
    return t || clean(el.textContent);
  };
  const visible = (el) => {
    try {
      const b = el.getBoundingClientRect();
      if (b.width === 0 && b.height === 0) return false;
    } catch (e) {}
    return true;
  };
"""

# ---------------------------------------------------------------- section

_SECTION_JS = r"""
(args) => {
""" + _JS_HELPERS + r"""
  const want = clean(args.heading).toLowerCase();
  const withSubs = args.include_subsections !== false;
  const nodes = Array.from(document.querySelectorAll(HEADING_SEL));
  const norm = (el) => strip(el.innerText).toLowerCase();

  // Exact, then prefix, then contains -- so "History" does not lose to
  // "History of the field" on a page carrying both.
  const start = nodes.find(el => norm(el) === want)
             || nodes.find(el => norm(el).startsWith(want))
             || nodes.find(el => norm(el).includes(want));
  if (!start) {
    return { found: false,
             headings: nodes.filter(visible).map(el => strip(el.innerText))
                            .filter(Boolean).slice(0, 60) };
  }

  const startLevel = level(start);
  // WITH subsections: a deeper heading belongs to this section, so only a
  // sibling-or-shallower one ends it. WITHOUT: the very next heading ends
  // it, whatever its level.
  const ends = (el) => withSubs ? (level(el) <= startLevel) : true;

  // Walk the document in order from the heading. Using a TreeWalker
  // rather than nextElementSibling because a heading is frequently
  // wrapped -- <section><h2>..</h2><p>..</p></section> -- and sibling
  // walking from the <h2> finds the <p> but never leaves the wrapper,
  // while sibling walking from the wrapper misses the <p> entirely.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  walker.currentNode = start;

  const parts = [];
  const subs = [];
  const seen = new Set();
  let node = walker.nextNode();
  while (node) {
    if (node.matches && node.matches(HEADING_SEL)) {
      if (ends(node)) break;
      subs.push({ level: level(node), text: strip(node.innerText) });
      node = walker.nextNode();
      continue;
    }
    // Only take text from a node whose ancestors we have not already
    // taken, or every paragraph is counted once per level of nesting.
    let covered = false;
    for (let p = node.parentElement; p; p = p.parentElement) {
      if (seen.has(p)) { covered = true; break; }
    }
    if (!covered) {
      const t = clean(node.innerText);
      if (t) { parts.push(t); seen.add(node); }
    }
    node = walker.nextNode();
  }

  return { found: true, heading: strip(start.innerText), level: startLevel,
           include_subsections: withSubs, subsections: subs.slice(0, 40),
           text: parts.join('\n') };
}
"""

# -------------------------------------------------------------------- toc

_TOC_JS = r"""
() => {
""" + _JS_HELPERS + r"""
  const here = location.href.split('#')[0];
  const isFragment = (a) => {
    const raw = a.getAttribute('href') || '';
    if (raw.startsWith('#') && raw.length > 1) return true;
    // Same page, different fragment: "/wiki/Page#History" while on
    // /wiki/Page. Common on server-rendered contents lists.
    const i = raw.indexOf('#');
    if (i <= 0) return false;
    try { return new URL(raw, location.href).href.split('#')[0] === here; }
    catch (e) { return false; }
  };

  // How deeply a link is nested in lists INSIDE the container. That is
  // what a contents list uses to express hierarchy.
  const depthIn = (el, root) => {
    let d = 0;
    for (let p = el.parentElement; p && p !== root; p = p.parentElement) {
      const t = (p.tagName || '').toUpperCase();
      if (t === 'UL' || t === 'OL') d++;
    }
    return d;
  };

  // Candidate containers, best signal first. Every one of these is an
  // HTML/ARIA convention rather than a fact about any particular site.
  const seen = new Set();
  const candidates = [];
  const add = (el, why, bonus) => {
    if (!el || seen.has(el) || !visible(el)) return;
    seen.add(el);
    candidates.push({ el: el, why: why, bonus: bonus || 0 });
  };
  document.querySelectorAll('[role=doc-toc]').forEach(el => add(el, 'role=doc-toc', 40));
  document.querySelectorAll('nav[aria-label],[role=navigation][aria-label]').forEach(el => {
    if (/contents|toc|on this page|in this article|jump to/i.test(el.getAttribute('aria-label') || ''))
      add(el, 'aria-label names a contents list', 30);
  });
  document.querySelectorAll('[id*=toc i],[class*=toc i]').forEach(el => add(el, 'id/class names toc', 20));
  document.querySelectorAll('nav').forEach(el => add(el, 'nav element', 5));
  document.querySelectorAll('ul,ol').forEach(el => add(el, 'list of same-page links', 0));

  let best = null, bestScore = 0, bestWhy = '';
  candidates.forEach(c => {
    const links = Array.from(c.el.querySelectorAll('a[href]')).filter(visible);
    if (links.length < 3) return;
    const frag = links.filter(isFragment);
    // A contents list is MOSTLY same-page links. A site nav is mostly not.
    const share = frag.length / links.length;
    if (share < 0.6 || frag.length < 3) return;
    // Prefer the container that covers more of the page's own anchors,
    // and break ties with the structural signal that found it.
    const score = frag.length * share * 10 + c.bonus;
    if (score > bestScore) {
      bestScore = score; best = { el: c.el, links: frag }; bestWhy = c.why;
    }
  });

  if (!best) return { found: false };

  const items = best.links.map(a => {
    const raw = a.getAttribute('href') || '';
    return { text: stripEl(a),
             anchor: raw.slice(raw.indexOf('#')),
             depth: depthIn(a, best.el) };
  }).filter(it => it.text);

  // Normalise depth so the shallowest item is level 1 whatever the
  // container's own wrapper nesting happens to be.
  const minD = items.reduce((m, it) => Math.min(m, it.depth), 99);
  items.forEach(it => { it.level = it.depth - minD + 1; delete it.depth; });
  return { found: true, why: bestWhy, items: items };
}
"""

# -------------------------------------------------------------- outline

_OUTLINE_JS = r"""
() => {
""" + _JS_HELPERS + r"""
  const out = [];
  document.querySelectorAll(HEADING_SEL).forEach(el => {
    if (!visible(el)) return;
    const text = strip(el.innerText);
    if (text) out.push({ level: level(el), text: text.slice(0, 120) });
  });
  return out;
}
"""

# ------------------------------------------------------------ structures

_STRUCTURE_JS = r"""
() => {
""" + _JS_HELPERS + r"""
  const out = { headings: 0, sections: [], lists: [], tables: [], cards: [] };

  document.querySelectorAll(HEADING_SEL).forEach(el => {
    if (!visible(el)) return;
    out.headings++;
    const t = strip(el.innerText);
    if (t) out.sections.push({ level: level(el), text: t.slice(0, 100) });
  });

  document.querySelectorAll('ul,ol,dl').forEach(el => {
    if (!visible(el)) return;
    // A list nested inside another list is part of that one, not its own.
    if (el.parentElement && el.parentElement.closest('ul,ol,dl')) return;
    const items = Array.from(el.children)
      .filter(c => /^(LI|DT|DD)$/.test(c.tagName || ''))
      .map(c => clean(c.innerText)).filter(Boolean);
    if (items.length >= 3) {
      out.lists.push({ kind: (el.tagName || '').toLowerCase(),
                       count: items.length,
                       sample: items.slice(0, 5).map(s => s.slice(0, 80)) });
    }
  });

  document.querySelectorAll('table').forEach((el, i) => {
    if (!visible(el)) return;
    const rows = el.querySelectorAll('tr').length;
    const cols = el.querySelector('tr')
      ? el.querySelector('tr').querySelectorAll('th,td').length : 0;
    const head = Array.from(el.querySelectorAll('th')).slice(0, 8)
      .map(th => clean(th.innerText)).filter(Boolean);
    if (rows >= 2) out.tables.push({ index: i, rows: rows, cols: cols, headers: head });
  });

  // Repeated card groups: sibling elements sharing a class signature.
  const groups = new Map();
  document.querySelectorAll('main *, body *').forEach(el => {
    if (!el.parentElement || !el.className || typeof el.className !== 'string') return;
    const sig = el.parentElement.tagName + '>' + el.tagName + '.' +
                el.className.trim().split(/\s+/).slice(0, 2).join('.');
    if (!groups.has(sig)) groups.set(sig, []);
    groups.get(sig).push(el);
  });
  const cards = [];
  groups.forEach((els, sig) => {
    if (els.length < 3) return;
    const texts = els.map(e => clean(e.innerText)).filter(t => t.length > 25);
    if (texts.length < 3) return;
    const linked = els.filter(e => e.querySelector('a[href]')).length / els.length;
    if (linked < 0.5) return;
    cards.push({ signature: sig.slice(0, 60), count: els.length,
                 sample: texts[0].slice(0, 120) });
  });
  cards.sort((a, b) => b.count - a.count);
  out.cards = cards.slice(0, 6);
  return out;
}
"""


# ------------------------------------------------------------- plumbing

def _session(args: Dict[str, Any]):
    from backend.app.browser.task_flow import get_manager
    token = str(args.get("session_token") or "").strip()
    if not token:
        return None, "no session_token given"
    session = get_manager().get(token)
    if not session:
        return None, ("no such browser session — it may have finished, failed, "
                      "timed out, or was never opened via action.browser_navigate")
    return session, None


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def _extract_section_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return f"(browser_extract_section failed: {err})"
    heading = str(args.get("heading") or "").strip()
    if not heading:
        return "(browser_extract_section failed: no heading given)"
    with_subs = _truthy(args.get("include_subsections"), True)

    from backend.app.browser.policy import wrap_untrusted
    from backend.app.browser.task_flow import _fp

    try:
        res = session.page.evaluate(
            _SECTION_JS, {"heading": heading, "include_subsections": with_subs}) or {}
        page_url = session.page.url
    except Exception as exc:  # noqa: BLE001
        return f"(browser_extract_section failed: {exc})"

    if not res.get("found"):
        listed = "\n".join(f"  - {h}" for h in (res.get("headings") or [])[:40])
        return (
            f"(no heading matching {heading!r} on {page_url}. This is a fact "
            f"about the HEADING you asked for, not about the page — do not "
            f"report the section as missing without checking this list. The "
            f"headings this page actually has:\n{listed or '  (none found)'})"
        )

    text = str(res.get("text") or "").strip()
    subs = res.get("subsections") or []
    sub_line = ""
    if subs:
        sub_line = ("Subsections included: "
                    + ", ".join(str(s.get("text")) for s in subs[:12]) + "\n")

    if not text:
        return (f"[section — {res.get('heading')} — {page_url}]\n{_fp(session.page)}"
                f"(the heading is on the page but carries no text before the "
                f"next heading. It is probably a container for subsections — "
                f"call action.browser_outline to see what sits under it.)")

    cut = ""
    if len(text) > MAX_SECTION_CHARS:
        cut = (f"\n[…SECTION TRUNCATED at {MAX_SECTION_CHARS} of {len(text)} "
               f"chars. You are NOT seeing all of it.]")
    return wrap_untrusted(
        f"[section — {res.get('heading')} (h{res.get('level')}) — {page_url}]\n"
        f"{_fp(session.page)}{sub_line}{text[:MAX_SECTION_CHARS]}{cut}",
        page_url,
    )


def _extract_toc_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return f"(browser_extract_toc failed: {err})"

    from backend.app.browser.policy import wrap_untrusted

    try:
        res = session.page.evaluate(_TOC_JS) or {}
        page_url = session.page.url
    except Exception as exc:  # noqa: BLE001
        return f"(browser_extract_toc failed: {exc})"

    if res.get("found"):
        items = res.get("items") or []
        lines = [f"[table of contents — {page_url}]",
                 f"{len(items)} entr(y/ies), in page order. Found by: "
                 f"{res.get('why')}. Indentation is nesting depth.", ""]
        for i, it in enumerate(items[:MAX_OUTLINE_ITEMS], 1):
            lvl = max(1, min(6, int(it.get("level") or 1)))
            lines.append(f"{i:>3}. {'  ' * (lvl - 1)}{it.get('text')}"
                         f"   ({it.get('anchor')})")
        return wrap_untrusted("\n".join(lines), page_url)

    # No contents WIDGET. The headings are still the page's contents, so
    # fall back rather than reporting that the page has no structure --
    # plenty of pages have sections and publish no TOC.
    try:
        heads = session.page.evaluate(_OUTLINE_JS) or []
    except Exception as exc:  # noqa: BLE001
        return f"(browser_extract_toc failed: {exc})"
    if not heads:
        return (f"(no table of contents and no headings on {page_url}. The "
                f"page may build its structure without heading markup — read "
                f"it with action.browser_extract instead.)")
    lines = [f"[table of contents — {page_url}]",
             f"This page publishes no contents widget, so this is its HEADING "
             f"OUTLINE instead — {len(heads)} heading(s) in page order.", ""]
    for i, it in enumerate(heads[:MAX_OUTLINE_ITEMS], 1):
        lvl = max(1, min(6, int(it.get("level") or 2)))
        lines.append(f"{i:>3}. {'  ' * (lvl - 1)}{it.get('text')}")
    return wrap_untrusted("\n".join(lines), page_url)


def _outline_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return f"(browser_outline failed: {err})"

    from backend.app.browser.policy import wrap_untrusted

    try:
        items = session.page.evaluate(_OUTLINE_JS) or []
        page_url = session.page.url
    except Exception as exc:  # noqa: BLE001
        return f"(browser_outline failed: {exc})"

    if not items:
        return (f"(no headings found on {page_url}. The page may build its "
                f"structure without heading markup — read it with "
                f"action.browser_extract instead.)")

    shown = items[:MAX_OUTLINE_ITEMS]
    lines = [f"[outline — {page_url}]",
             f"{len(items)} heading(s), in the order the page lists them. "
             f"Indentation is the heading level.", ""]
    for i, it in enumerate(shown, 1):
        lvl = max(1, min(6, int(it.get("level") or 2)))
        lines.append(f"{i:>3}. {'  ' * (lvl - 1)}{it.get('text')}")
    if len(items) > len(shown):
        lines.append(f"… {len(items) - len(shown)} further heading(s) not shown.")
    return wrap_untrusted("\n".join(lines), page_url)


def _structure_impl(args: Dict[str, Any]) -> str:
    session, err = _session(args)
    if err:
        return f"(browser_page_structure failed: {err})"

    from backend.app.browser.policy import wrap_untrusted

    try:
        s = session.page.evaluate(_STRUCTURE_JS) or {}
        page_url = session.page.url
    except Exception as exc:  # noqa: BLE001
        return f"(browser_page_structure failed: {exc})"

    lines = [f"[structure — {page_url}]",
             "What this page is made of — use it to pick the right reader.", ""]

    secs = s.get("sections") or []
    if secs:
        lines.append(f"SECTIONS: {len(secs)} heading(s). Read one with "
                     f"action.browser_extract_section(heading=...).")
        for it in secs[:12]:
            lvl = max(1, min(6, int(it.get("level") or 2)))
            lines.append(f"   {'  ' * (lvl - 1)}- {it.get('text')}")
        if len(secs) > 12:
            lines.append(f"   … {len(secs) - 12} more (action.browser_outline "
                         f"lists them all)")
        lines.append("")

    tabs = s.get("tables") or []
    if tabs:
        lines.append(f"TABLES: {len(tabs)}. Read with "
                     f"action.browser_extract_table (omit table_index for the "
                     f"data one).")
        for t in tabs[:6]:
            hdr = ", ".join(t.get("headers") or [])[:80]
            lines.append(f"   - table {t.get('index')}: {t.get('rows')} rows × "
                         f"{t.get('cols')} cols{(' | ' + hdr) if hdr else ''}")
        lines.append("")

    lists = s.get("lists") or []
    if lists:
        lines.append(f"LISTS: {len(lists)}.")
        for l in lists[:6]:
            lines.append(f"   - <{l.get('kind')}> {l.get('count')} items: "
                         f"{'; '.join(l.get('sample') or [])[:100]}")
        lines.append("")

    cards = s.get("cards") or []
    if cards:
        lines.append(f"REPEATED CARD GROUPS: {len(cards)}. Read with "
                     f"action.browser_extract_records.")
        for c in cards[:4]:
            lines.append(f"   - {c.get('count')}× {c.get('signature')}: "
                         f"{str(c.get('sample'))[:80]}")
        lines.append("")

    if len(lines) <= 3:
        lines.append("No headings, tables, lists or repeated groups found. "
                     "This page is probably plain prose — read it with "
                     "action.browser_extract.")
    return wrap_untrusted("\n".join(lines)[:MAX_STRUCTURE_CHARS], page_url)


def _on_thread(fn, args, name):
    from backend.app.browser.task_flow import _on_browser_thread
    return _on_browser_thread(fn, args, name)


_TOKEN_PARAM = {"name": "session_token", "type": "string", "required": True,
                "description": "Token from action.browser_navigate."}

BROWSER_EXTRACT_SECTION_SPEC = ActionSpec(
    name="browser_extract_section",
    description=(
        "Extract ONE named section — the text under a specific heading, "
        "stopping where that section ends. Use this whenever the task names "
        "a PART of a page ('the History section', 'the Requirements "
        "heading') rather than reading the whole page and hunting through "
        "it. NOTE: navigating to a '#Section' URL only SCROLLS the browser — "
        "it does not change what action.browser_extract returns, so this is "
        "the only tool that actually narrows the read. If the heading is not "
        "found you are given the list of headings that are."
    ),
    parameters=[
        _TOKEN_PARAM,
        {"name": "heading", "type": "string", "required": True,
         "description": "The heading to read, worded as the page words it."},
        {"name": "include_subsections", "type": "boolean", "required": False,
         "description": ("Default true: deeper headings under this one are "
                         "part of it. Set false for the text directly under "
                         "this heading only, stopping at the next heading of "
                         "any level.")},
    ],
    handler=lambda a: _on_thread(_extract_section_impl, a, "browser_extract_section"),
    preview=lambda a: f"Extract the {str(a.get('heading'))[:40]!r} section",
    mutating=False,
    capability="web.page.read",
)

BROWSER_EXTRACT_TOC_SPEC = ActionSpec(
    name="browser_extract_toc",
    description=(
        "Extract the page's TABLE OF CONTENTS as the page itself publishes "
        "it — found by structure (a contents nav, a run of same-page "
        "fragment links, role=doc-toc, an aria-label naming it), never from "
        "any site's API. Order and nesting are preserved, and each entry "
        "carries its anchor. If the page publishes no contents widget you "
        "get its heading outline instead. Use this for 'what does this page "
        "cover', 'its contents', or to learn the exact wording of a heading "
        "before calling action.browser_extract_section."
    ),
    parameters=[_TOKEN_PARAM],
    handler=lambda a: _on_thread(_extract_toc_impl, a, "browser_extract_toc"),
    preview=lambda a: "Extract the table of contents",
    mutating=False,
    capability="web.page.read",
)

BROWSER_OUTLINE_SPEC = ActionSpec(
    name="browser_outline",
    description=(
        "List every heading on the page in order, with its level. The "
        "page's structure as its markup declares it, rather than as a "
        "contents widget presents it — use action.browser_extract_toc when "
        "you want what the page shows a reader, and this when you want "
        "everything."
    ),
    parameters=[_TOKEN_PARAM],
    handler=lambda a: _on_thread(_outline_impl, a, "browser_outline"),
    preview=lambda a: "Read the page outline",
    mutating=False,
    capability="web.page.read",
)

BROWSER_PAGE_STRUCTURE_SPEC = ActionSpec(
    name="browser_page_structure",
    description=(
        "Report what the page is MADE OF — how many sections, tables, "
        "lists and repeated card groups it has, with a sample of each — and "
        "which reader to use for them. Call this when a page did not give "
        "you what you expected and you need to know what shape it is, "
        "instead of trying each extraction tool in turn."
    ),
    parameters=[_TOKEN_PARAM],
    handler=lambda a: _on_thread(_structure_impl, a, "browser_page_structure"),
    preview=lambda a: "Inspect the page's structure",
    mutating=False,
    capability="web.page.read",
)

ALL_SPECS = [
    BROWSER_EXTRACT_SECTION_SPEC,
    BROWSER_EXTRACT_TOC_SPEC,
    BROWSER_OUTLINE_SPEC,
    BROWSER_PAGE_STRUCTURE_SPEC,
]
