"""Deterministic markdown -> structured-content converters.

No LLM call. These walk a FINISHED, already-well-written deliverable
(the specialist's normal synthesis output) and reshape it into the
structured args create_pptx/create_xlsx expect. create_docx doesn't
need one of these — it already accepts raw markdown text directly.

Why this exists: the alternative (asking the LLM to freehand full
slide/table content inside the pre-flight tool-call step) produced
empty placeholder slides — confirmed live: a request for a 5-slide
deck came back as 5 slides literally titled "Slide 1".."Slide 5" with
zero bullets, because that pre-flight call happens before the
specialist has done any real writing and is token-capped besides.
Converting the deliverable AFTER it's written sidesteps both problems.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_H1_RE = re.compile(r"^#\s+(.+)$")
_H2_RE = re.compile(r"^##\s+(.+)$")
_H3_RE = re.compile(r"^###\s+(.+)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.+)$")
_TABLE_SEP_RE = re.compile(r"^\|?[\s:\-|]+\|?$")

MAX_SLIDES = 15
MAX_BULLETS_PER_SLIDE = 8
MAX_BULLET_CHARS = 220


def markdown_to_pptx_slides(
    markdown_text: str, fallback_title: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Walk the deliverable's headings/bullets/paragraphs into
    {heading, bullets} slide entries. The first H1 becomes the deck
    title (not its own slide); every H2 starts a new slide; H3s and
    bare paragraphs fold into bullets on the current slide. Tables and
    fenced code blocks are skipped — too dense for slide bullets."""
    lines = markdown_text.replace("\r\n", "\n").split("\n")
    title = fallback_title
    seen_title = False
    slides: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    in_code_block = False

    def flush() -> None:
        if current and (current["bullets"] or current["heading"]):
            slides.append(current)

    for raw in lines:
        stripped = raw.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue

        h1 = _H1_RE.match(stripped)
        if h1:
            if not seen_title:
                title = h1.group(1).strip()
                seen_title = True
                continue
            flush()
            current = {"heading": h1.group(1).strip(), "bullets": []}
            continue

        h2 = _H2_RE.match(stripped)
        if h2:
            flush()
            current = {"heading": h2.group(1).strip(), "bullets": []}
            continue

        if current is None:
            current = {"heading": title, "bullets": []}

        if len(current["bullets"]) >= MAX_BULLETS_PER_SLIDE:
            continue  # slide's full — skip extra content rather than overflow it

        h3 = _H3_RE.match(stripped)
        if h3:
            current["bullets"].append(h3.group(1).strip()[:MAX_BULLET_CHARS])
            continue

        if stripped.startswith(("- ", "* ")):
            current["bullets"].append(stripped[2:].strip()[:MAX_BULLET_CHARS])
            continue

        numbered = _NUMBERED_RE.match(stripped)
        if numbered:
            current["bullets"].append(numbered.group(1).strip()[:MAX_BULLET_CHARS])
            continue

        if stripped.startswith("|") or _TABLE_SEP_RE.match(stripped):
            continue  # table row/separator — skip, too dense for a slide bullet

        if stripped.startswith(("---", "===")):
            continue

        clean = stripped.replace("**", "").replace("__", "")
        current["bullets"].append(clean[:MAX_BULLET_CHARS])

    flush()
    return title, slides[:MAX_SLIDES]


def markdown_to_xlsx_table(
    markdown_text: str,
) -> Tuple[Optional[List[str]], Optional[List[List[Any]]]]:
    """Find the FIRST GFM table in the text and return (headers, rows).
    Returns (None, None) if no table is present — converting prose to a
    spreadsheet doesn't make sense, so the caller should skip xlsx
    generation entirely in that case rather than emit an empty file."""
    lines = markdown_text.replace("\r\n", "\n").split("\n")
    for i in range(len(lines) - 1):
        line = lines[i].strip()
        sep = lines[i + 1].strip()
        if not line.startswith("|"):
            continue
        if not re.match(r"^\|?[\s:\-|]+\|?$", sep):
            continue
        headers = [c.strip() for c in line.strip("|").split("|")]
        rows: List[List[Any]] = []
        j = i + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
            # Pad/truncate to header width so openpyxl doesn't choke on ragged rows.
            if len(cells) < len(headers):
                cells += [""] * (len(headers) - len(cells))
            elif len(cells) > len(headers):
                cells = cells[: len(headers)]
            rows.append(cells)
            j += 1
        return headers, rows
    return None, None
