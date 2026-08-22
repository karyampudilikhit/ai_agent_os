"""create_xlsx — generate a real Excel workbook from tabular data.

Same sandboxed-workspace trust tier and non-mutating rationale as
create_pptx (see that module's docstring).
"""

from __future__ import annotations

from typing import Any, Dict

from backend.app.actions.action_registry import ActionSpec
from backend.app.actions.builtin._workspace import resolve_within, workspace_root

MAX_ROWS = 5000


def _handler(args: Dict[str, Any]) -> str:
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError:
        return "(openpyxl not installed on the server — cannot create .xlsx. Run: pip install openpyxl)"

    filename = str(args.get("filename") or "sheet.xlsx").strip()
    if not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"
    sheet_name = (str(args.get("sheet_name") or "Sheet1").strip() or "Sheet1")[:31]
    headers = args.get("headers") or []
    rows = args.get("rows") or []
    if not isinstance(headers, list) or not headers:
        return "(missing 'headers' — expected an array of column-name strings)"
    if not isinstance(rows, list):
        return "(missing 'rows' — expected an array of row arrays)"

    root = workspace_root()
    try:
        target = resolve_within(root, filename)
    except ValueError as exc:
        return f"(rejected: {exc})"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append([str(h) for h in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True)

    row_count = 0
    for row in rows[:MAX_ROWS]:
        if isinstance(row, list):
            ws.append(["" if c is None else c for c in row])
            row_count += 1

    for col_cells in ws.columns:
        lengths = [len(str(c.value)) for c in col_cells if c.value is not None]
        width = max(lengths, default=8) + 2
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(width, 10), 50)

    target.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(target))
    rel = target.relative_to(root)
    url = f"/api/workspace/download/{rel.as_posix()}"
    return f"Created {rel} ({row_count} row{'s' if row_count != 1 else ''}). Download: {url}"


def _preview(args: Dict[str, Any]) -> str:
    n = len(args.get("rows") or [])
    return f"Create XLSX: {args.get('filename') or 'sheet.xlsx'} — {n} row(s)"


SPEC = ActionSpec(
    name="create_xlsx",
    description=(
        "Create a real Excel (.xlsx) file in the workspace from tabular data. Writes "
        "only inside the sandboxed workspace — no approval needed. Arguments: filename "
        "(e.g. 'model.xlsx'), sheet_name (optional), headers (array of column-name "
        "strings), rows (array of arrays — each inner array is one row, same column "
        "order as headers, max 5000 rows)."
    ),
    parameters=[
        {"name": "filename", "type": "string", "description": "Output filename, e.g. 'model.xlsx'", "required": True},
        {"name": "sheet_name", "type": "string", "description": "Sheet tab name (optional, default Sheet1)"},
        {"name": "headers", "type": "array", "description": "Column header strings", "required": True},
        {"name": "rows", "type": "array", "description": "Array of row arrays (each matches headers order)", "required": True},
    ],
    handler=_handler,
    preview=_preview,
    mutating=False,
    planner_excluded=True,
)
