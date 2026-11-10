"""Read one AOI workbook: defects per Verification code.

Only the ``Defects`` sheet is opened, and from it only the ``VERIFICATION`` and
``DEFECT_ID`` columns are used. Columns are found by their header name (case
and spaces do not matter); a file whose headers are not recognised falls back
to the fixed AOI column order, exactly like the Faster AOI app:

    DEFECT_ID, DEFECT_TYPE, X_COORDINATES, Y_COORDINATES, UNIT_INDEX_X,
    UNIT_INDEX_Y, ..., VERIFICATION, ACCEPT
"""

from __future__ import annotations

import zipfile
from datetime import datetime

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from aoi_metadata.models import NOT_VERIFIED, FileRead, FoundFile, IssueRow

POSITIONAL = ("DEFECT_ID", "DEFECT_TYPE", "X_COORDINATES", "Y_COORDINATES",
              "UNIT_INDEX_X", "UNIT_INDEX_Y")  # fmt: skip
FINAL_INSPECTION_NAMES = {"FINAL INSPECTION", "ACCEPTABLE", "ACCEPT", "ACCPET"}


def header_key(value: object) -> str:
    """'  Defect Id ' -> 'DEFECT_ID'."""
    return "_".join(str(value or "").strip().upper().replace("-", " ").split())


def code(value: object) -> str:
    """A Verification cell as its code: ' cu41' -> 'CU41', 1.0 -> '1', empty -> 'Not verified'."""
    if value is None:
        return NOT_VERIFIED
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value != value:  # NaN
            return NOT_VERIFIED
        if value.is_integer():
            value = int(value)
    text = str(value).strip().upper()
    return text or NOT_VERIFIED


def _id(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "" if value is None else str(value).strip()


def _columns(header: tuple[object, ...]) -> tuple[int | None, int | None, bool]:
    """(Verification column, DEFECT_ID column, positional fallback used)."""
    keys = [header_key(h) for h in header]
    verification = keys.index("VERIFICATION") if "VERIFICATION" in keys else None
    defect_id = keys.index("DEFECT_ID") if "DEFECT_ID" in keys else None
    if verification is not None or "UNIT_INDEX_X" in keys:
        return verification, defect_id, False
    # Headers not recognised: the fixed AOI order (as the Faster AOI app does).
    width = len(keys)
    while width and keys[width - 1] == "":
        width -= 1
    if width >= len(POSITIONAL) + 2:
        return width - 2, 0, True
    if width == len(POSITIONAL) + 1:
        return width - 1, 0, True
    return None, (0 if width >= len(POSITIONAL) else None), width >= len(POSITIONAL)


def _sheet(names: list[str], wanted: str) -> str | None:
    lowered = {n.strip().lower(): n for n in names}
    return lowered.get(wanted.strip().lower())


def read_file(found: FoundFile, defects_sheet: str = "Defects") -> FileRead:
    path = found.path
    result = FileRead()

    def issue(severity: str, category: str, message: str) -> None:
        result.issues.append(
            IssueRow(severity, category, message, path.name, "", found.side.lot_type,
                     found.side.project, found.side.lot)
        )  # fmt: skip

    try:
        result.measured_at = datetime.fromtimestamp(path.stat().st_mtime)
        workbook = load_workbook(path, read_only=True, data_only=True)
        # When the AOI wrote the workbook: its own 'created' time, which stays the same when
        # someone opens and saves the file again (the file date is only the fallback).
        created = workbook.properties.created
        if isinstance(created, datetime):
            result.measured_at = created
    except (OSError, InvalidFileException, zipfile.BadZipFile, KeyError, ValueError) as exc:
        issue("ERROR", "unreadable_workbook", f"Workbook cannot be opened: {exc}")
        result.failed = True
        return result
    try:
        sheet_name = _sheet(workbook.sheetnames, defects_sheet)
        if sheet_name is None:
            issue(
                "ERROR",
                "missing_sheet",
                f"No '{defects_sheet}' sheet (sheets: {', '.join(workbook.sheetnames)}).",
            )
            result.failed = True
            return result
        rows = workbook[sheet_name].iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            issue("WARNING", "empty_sheet", f"The '{sheet_name}' sheet is empty.")
            return result
        verification, defect_id, positional = _columns(tuple(header))
        if positional:
            issue(
                "WARNING",
                "positional_columns",
                "Column names not recognised; the fixed AOI column order was used.",
            )
        if verification is None:
            issue(
                "WARNING",
                "no_verification_column",
                f"No VERIFICATION column; every defect is counted as '{NOT_VERIFIED}'.",
            )
        counts: dict[str, int] = {}
        for row in rows:
            if row is None or all(v is None or str(v).strip() == "" for v in row):
                continue
            value = row[verification] if verification is not None and verification < len(row) \
                else None  # fmt: skip
            name = code(value)
            counts[name] = counts.get(name, 0) + 1
            if defect_id is not None and defect_id < len(row):
                ident = _id(row[defect_id])
                if ident:
                    result.defect_ids.append(ident)
        result.counts = counts
    except Exception as exc:  # a damaged sheet must not stop the run
        issue("ERROR", "unreadable_workbook", f"Workbook cannot be read: {exc}")
        result.failed = True
        result.counts = {}
        result.defect_ids = []
    finally:
        workbook.close()
    return result
