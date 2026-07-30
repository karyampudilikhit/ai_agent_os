"""create_pptx — generate a real PowerPoint file from structured content.

Writes only inside the sandboxed workspace (see _workspace.py) —
nothing leaves the app, nothing is sent anywhere. Non-mutating: it
auto-runs without an approval tap, same trust tier as read_file. The
rationale for that (rather than matching write_file's mutating=True)
is that a locally-generated file has no external side effect and is
trivially undoable — the founder can delete it. If that's too loose
for your taste, flip mutating=True below; nothing else needs to change.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.builtin._workspace import resolve_within, workspace_root

MAX_SLIDES = 40
MAX_BULLETS_PER_SLIDE = 12


def _handler(args: Dict[str, Any]) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        return "(python-pptx not installed on the server — cannot create .pptx. Run: pip install python-pptx)"

    filename = str(args.get("filename") or "deck.pptx").strip()
    if not filename.lower().endswith(".pptx"):
        filename += ".pptx"
    title = str(args.get("title") or "Untitled Deck").strip()
    subtitle = str(args.get("subtitle") or "").strip()
    slides = args.get("slides") or []
    if not isinstance(slides, list) or not slides:
        return "(missing 'slides' — expected an array of {heading, bullets: [string,...]})"

    root = workspace_root()
    try:
        target = resolve_within(root, filename)
    except ValueError as exc:
        return f"(rejected: {exc})"

    prs = Presentation()

    title_layout = prs.slide_layouts[0]
    title_slide = prs.slides.add_slide(title_layout)
    title_slide.shapes.title.text = title
    if subtitle and len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = subtitle

    bullet_layout = prs.slide_layouts[1]
    made = 0
    for entry in slides[:MAX_SLIDES]:
        if not isinstance(entry, dict):
            continue
        heading = str(entry.get("heading") or "").strip()
        bullets = entry.get("bullets") or []
        if not isinstance(bullets, list):
            bullets = []
        s = prs.slides.add_slide(bullet_layout)
        s.shapes.title.text = heading or f"Slide {made + 1}"
        body = s.placeholders[1].text_frame
        body.clear()
        first = True
        for b in bullets[:MAX_BULLETS_PER_SLIDE]:
            text = str(b).strip()
            if not text:
                continue
            if first:
                body.text = text
                first = False
            else:
                p = body.add_paragraph()
                p.text = text
        made += 1

    target.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(target))
    rel = target.relative_to(root)
    url = f"/api/workspace/download/{rel.as_posix()}"
    return f"Created {rel} ({made} content slide{'s' if made != 1 else ''}). Download: {url}"


def _preview(args: Dict[str, Any]) -> str:
    n = len(args.get("slides") or [])
    title = args.get("title") or "(untitled)"
    return f"Create PPTX: {title} — {n} slide(s)"


SPEC = ActionSpec(
    name="create_pptx",
    description=(
        "Create a real PowerPoint (.pptx) file in the workspace from structured content. "
        "Writes only inside the sandboxed workspace, no approval needed. Arguments: "
        "filename (e.g. 'pitch.pptx'), title (title-slide headline), subtitle (optional), "
        "slides (array of {heading, bullets: [string,...]} — one object per content slide, "
        "max 40 slides, max 12 bullets each)."
    ),
    parameters=[
        {"name": "filename", "type": "string", "description": "Output filename, e.g. 'pitch.pptx'", "required": True},
        {"name": "title", "type": "string", "description": "Title slide headline", "required": True},
        {"name": "subtitle", "type": "string", "description": "Title slide subtitle (optional)"},
        {"name": "slides", "type": "array", "description": "Array of {heading, bullets: [string,...]}", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=False,
)
