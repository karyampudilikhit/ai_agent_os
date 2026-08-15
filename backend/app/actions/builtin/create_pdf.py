"""create_pdf — generate a real PDF from markdown-ish content.

Same sandboxed-workspace trust tier and non-mutating rationale as
create_docx/create_pptx/create_xlsx, and the same light markdown subset
(# / ## / ### headings, '- '/'* ' bullets, '1. ' numbered lists, plain
paragraphs) so a specialist's already-written markdown deliverable
becomes a real .pdf without re-authoring it.

WHY THIS DIDN'T EXIST UNTIL NOW. A live end-to-end test asked Vision AI
for "a complete report... deliver the whole thing as a PDF file." The
team produced a real, coherent deliverable and no PDF, because no
create_pdf action existed anywhere in the system, and separately,
_maybe_generate_document's trigger-word lists had no pdf entry. Verified
directly: the exact same deliverable text produced a real downloadable
.docx when the task said "docx" and produced nothing at all, silently,
when it said "pdf".

Uses reportlab (already a project dependency, confirmed importable
inside run_python's exact sandboxed subprocess invocation). No new
dependency.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.builtin._workspace import resolve_within, workspace_root

_NUMBERED_RE = re.compile(r"^\d+\.\s+(.*)$")

# reportlab's base-14 Helvetica renders through WinAnsiEncoding (cp1252).
# A character outside that 256-slot table doesn't error -- it silently
# renders as a black .notdef box. Caught live: a Supervisor's synthesized
# prose used U+2011 (non-breaking hyphen) for "AI-enhanced"/"low-code",
# and U+202F (narrow no-break space) as the separator in "Zapier + Google
# Sheets". Both are silent corruption, and the space case is worse than a
# box: dropping it glued words into "Zapier+GoogleSheets", which looks
# fine and changes the meaning.
#
# Written as explicit \uXXXX escapes, not literal characters -- there are
# a dozen visually identical "space" codepoints and typing one directly
# into source is unverifiable, which is exactly the class of invisible
# mistake this function exists to catch.
_TYPOGRAPHY_MAP = {
    # Hyphen / dash / minus variants -> ASCII hyphen.
    "‐": "-", "‑": "-", "‒": "-", "−": "-",
    # Smart quotes -> straight quotes.
    "‘": "'", "’": "'", "‚": "'",
    "“": '"', "”": '"', "„": '"',
    "…": "...",
    # Space-LIKE characters MUST become a real space, never vanish.
    # Built programmatically from explicit codepoints: typing these as
    # literal characters is unverifiable (they are invisible and several
    # are visually identical), and doing so silently normalised U+202F --
    # the exact character that caused the bug -- into a plain ASCII space
    # when the file was written.
    **{chr(cp): " " for cp in (
        0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006,
        0x2007, 0x2008, 0x2009, 0x200A,   # en/em/thin/hair spaces
        0x202F,                            # narrow no-break space  <-- the live bug
        0x205F, 0x3000,                    # medium math, ideographic
    )},
    # Genuinely zero-width -- correctly disappear, unlike the above.
    **{chr(cp): "" for cp in (0x200B, 0x200C, 0x200D, 0xFEFF)},
    "⚠": "[!]",
    "️": "",
}


def _winansi_safe(text: str) -> str:
    """Map common LLM 'smart typography' to ASCII, then verify every
    remaining character against the ACTUAL encoding reportlab uses
    (cp1252 == WinAnsiEncoding) rather than guessing which Unicode
    variants happen to be covered. Anything still unencodable (emoji,
    symbols, non-Latin scripts) is dropped rather than left to render as
    an unexplained box."""
    out = []
    for ch in text:
        mapped = _TYPOGRAPHY_MAP.get(ch, ch)
        try:
            mapped.encode("cp1252")
            out.append(mapped)
        except UnicodeEncodeError:
            pass
    return "".join(out)


def _clean_inline(text: str) -> str:
    """Sanitize to the font's real character set, escape the characters
    reportlab's mini-XML parser is sensitive to, then convert markdown
    emphasis to its markup.

    Order matters: sanitize BEFORE escaping, since the &amp;/&lt;/&gt;
    sequences inserted here are pure ASCII and must survive untouched.
    """
    text = _winansi_safe(text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", text)
    return text


def _render_markdown_into_pdf(story, content: str, styles) -> None:
    from reportlab.platypus import Paragraph, Spacer, ListFlowable, ListItem

    bullets: list = []
    numbers: list = []

    def flush_bullets():
        if bullets:
            story.append(ListFlowable(
                [ListItem(Paragraph(_clean_inline(b), styles["Normal"])) for b in bullets],
                bulletType="bullet", leftIndent=18))
            bullets.clear()

    def flush_numbers():
        if numbers:
            story.append(ListFlowable(
                [ListItem(Paragraph(_clean_inline(n), styles["Normal"])) for n in numbers],
                bulletType="1", leftIndent=18))
            numbers.clear()

    for raw in content.split("\n"):
        s = raw.strip()
        if not s:
            flush_bullets(); flush_numbers(); story.append(Spacer(1, 8)); continue
        if s.startswith("### "):
            flush_bullets(); flush_numbers()
            story.append(Paragraph(_clean_inline(s[4:].strip()), styles["Heading3"]))
        elif s.startswith("## "):
            flush_bullets(); flush_numbers()
            story.append(Paragraph(_clean_inline(s[3:].strip()), styles["Heading2"]))
        elif s.startswith("# "):
            flush_bullets(); flush_numbers()
            story.append(Paragraph(_clean_inline(s[2:].strip()), styles["Heading1"]))
        elif s.startswith(("- ", "* ")):
            flush_numbers(); bullets.append(s[2:].strip())
        else:
            m = _NUMBERED_RE.match(s)
            if m:
                flush_bullets(); numbers.append(m.group(1).strip())
            else:
                flush_bullets(); flush_numbers()
                story.append(Paragraph(_clean_inline(s), styles["Normal"]))
    flush_bullets(); flush_numbers()


def _handler(args: Dict[str, Any]) -> str:
    try:
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
    except ImportError:
        return "(reportlab not installed on the server — cannot create .pdf. Run: pip install reportlab)"

    filename = str(args.get("filename") or "document.pdf").strip()
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "").strip()
    if not content:
        return "(missing 'content' — nothing to write)"

    root = workspace_root()
    try:
        target = resolve_within(root, filename)
    except ValueError as exc:
        return f"(rejected: {exc})"

    styles = getSampleStyleSheet()
    story = []
    if title:
        story.append(Paragraph(_clean_inline(title), styles["Title"]))
        story.append(Spacer(1, 14))
    _render_markdown_into_pdf(story, content, styles)

    target.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(target), pagesize=LETTER,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        title=title or filename,
    ).build(story)

    rel = target.relative_to(root)
    return f"Created {rel}. Download: /api/workspace/download/{rel.as_posix()}"


def _preview(args: Dict[str, Any]) -> str:
    n = len(str(args.get("content") or ""))
    label = args.get("title") or args.get("filename") or "(untitled)"
    return f"Create PDF: {label} — {n} chars"


SPEC = ActionSpec(
    name="create_pdf",
    description=(
        "Create a real PDF file in the workspace from markdown-ish text. "
        "Supports # / ## / ### headings, '- ' bullets, '1. ' numbered lists, and plain "
        "paragraphs. Writes only inside the sandboxed workspace — no approval needed. "
        "Arguments: filename (e.g. 'report.pdf'), title (optional doc title), content "
        "(the full markdown-ish body text)."
    ),
    parameters=[
        {"name": "filename", "type": "string", "description": "Output filename, e.g. 'report.pdf'", "required": True},
        {"name": "title", "type": "string", "description": "Optional document title (rendered as Title style)"},
        {"name": "content", "type": "string", "description": "Markdown-ish body text", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=False,
    # Same convention as create_docx/pptx/xlsx: hidden from the agentic
    # loop and fired post-synthesis instead. Asking the LLM to freehand a
    # whole document inside a token-capped pre-flight call produces empty
    # placeholder output -- the same failure mode the other three hit.
    planner_excluded=True,
)
