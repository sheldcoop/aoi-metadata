"""One run of the AOI export.

1. Look through every lot type folder (Shippable, Engineering) and every
   project for AOI side folders (``scan``).
2. Read only the workbooks that are new or changed since the last run; the
   rest come from the cache (``cache.json`` in the cache folder).
3. Build the tables and write them to the output folder:

   * ``AOI_Data.parquet``  long: one row per file and Verification code (for the app)
   * ``AOI_Files.parquet`` one row per workbook, also those without defects
   * ``AOI_Issues.parquet``, ``AOI_Runs.parquet``
   * ``AOI_Wide.xlsx``     one row per panel side, one column per Verification code
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from openpyxl import load_workbook

from aoi_metadata import __version__
from aoi_metadata.models import NOT_VERIFIED, SIDE_KEY, FileRead, FoundFile, IssueRow, Scope
from aoi_metadata.reader import read_file
from aoi_metadata.scan import find_lots, natural_key, scan_project, sort_text
from aoi_metadata.settings import Settings

logger = logging.getLogger(__name__)

CACHE_VERSION = 2
CACHE_FILE = "cache.json"
LOCK_FILE = "aoi_run.lock"
LOCK_MAX_AGE = 6 * 3600  # seconds: an older lock is left over from a crash

DATA = "AOI_Data"
FILES = "AOI_Files"
ISSUES = "AOI_Issues"
RUNS = "AOI_Runs"
WIDE = "AOI_Wide"

LOT_KEY = "Lot Key"
DATA_COLUMNS = (*SIDE_KEY, "Verification", "Count", "Source File", "Path", "Measured At",
                "Loaded At", LOT_KEY)  # fmt: skip
FILE_COLUMNS = (*SIDE_KEY, "Source File", "Defects", "Read OK", "Path", "Measured At",
                "Loaded At", LOT_KEY)  # fmt: skip
ISSUE_COLUMNS = ("Severity", "Category", "Lot Type", "Project", "Message", "Source File", "Path",
                 "Found At")  # fmt: skip
RUN_COLUMNS = ("Run At", "Mode", "Version", "Result", "Files Found", "Files Read",
               "Files Unchanged", "Files Removed", "Files Failed", "Side Folders", "Defects",
               "Issues", "Seconds")  # fmt: skip
LOT_NAME = "Lot Name"  # empty in the Excel file for you to fill in; kept on the next run
WIDE_FIXED = (*SIDE_KEY[:4], LOT_NAME, *SIDE_KEY[4:], "Files", "Total Defects", "Last Measured",
              "Folder")  # fmt: skip

Progress = Callable[[str], None]


class RunError(Exception):
    """The run cannot start or cannot write its tables."""


@dataclass
class RunOptions:
    full_reload: bool = False
    scope: Scope = field(default_factory=Scope)
    # Written to a test folder on this PC instead of the output folder.
    test: bool = False

    @property
    def mode(self) -> str:
        parts = ["test" if self.test else "normal", self.scope.describe()]
        if self.full_reload:
            parts.append("full reload")
        return ", ".join(parts)


@dataclass
class RunReport:
    run: dict[str, Any] = field(default_factory=dict)
    output: Path = Path()
    written: list[Path] = field(default_factory=list)


# -- places and lock ---------------------------------------------------------------------


def places(settings: Settings, options: RunOptions) -> tuple[Path, Path]:
    """(output folder, cache folder); a test run never touches the real ones."""
    if options.test:
        test = settings.cache / "test"
        return test / "output", test / "cache"
    return settings.output, settings.cache


class RunLock:
    """Stops a scheduled run and a run from the window writing at the same time."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / LOCK_FILE

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if age < LOCK_MAX_AGE:
                raise RunError(
                    f"Another AOI run is busy (lock file {self.path}). Wait for it to finish; "
                    f"delete the file only if no run is going on."
                )
        self.path.write_text(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}")
        return self

    def __exit__(self, *_: object) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


# -- cache ---------------------------------------------------------------------------------


def _load_cache(folder: Path) -> tuple[dict[str, dict[str, Any]], list[IssueRow]]:
    """(workbooks read before, folder issues found before)."""
    path = folder / CACHE_FILE
    if not path.is_file():
        return {}, []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Cache %s cannot be read; every file is read again.", path)
        return {}, []
    if not isinstance(loaded, dict) or loaded.get("version") != CACHE_VERSION:
        return {}, []
    files = loaded.get("files")
    try:
        issues = [IssueRow(**row) for row in loaded.get("scan_issues") or []]
    except TypeError:
        issues = []
    return (files if isinstance(files, dict) else {}), issues


def _save_cache(folder: Path, files: dict[str, dict[str, Any]], issues: list[IssueRow]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / CACHE_FILE
    temporary = path.with_suffix(".tmp")
    content = {
        "version": CACHE_VERSION,
        "files": files,
        "scan_issues": [dataclasses.asdict(i) for i in issues],
    }
    temporary.write_text(json.dumps(content), encoding="utf-8")
    temporary.replace(path)


def _in_scope(entry: dict[str, Any], scope: Scope) -> bool:
    side = entry["side"]  # SIDE_KEY order: lot type, project, part, lot, ...
    return scope.contains(side[0], side[1], side[3])


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def _entry(found: FoundFile, size: int, mtime: int, read: FileRead, loaded: datetime) -> dict:
    side = found.side
    return {
        "size": size,
        "mtime": mtime,
        "side": list(side.key()),
        "folder": str(side.folder),
        "file": found.path.name,
        "counts": read.counts,
        "ids": read.defect_ids,
        "failed": read.failed,
        "measured_at": read.measured_at.isoformat() if read.measured_at else None,
        "loaded_at": loaded.isoformat(),
        "issues": [
            [i.severity, i.category, i.message, i.source_file, i.lot_type, i.project, i.lot]
            for i in read.issues
        ],
    }


# -- scanning --------------------------------------------------------------------------------


def projects(settings: Settings) -> list[tuple[str, Path]]:
    """(lot type, project folder) of every project below the source root."""
    found: list[tuple[str, Path]] = []
    skip = {s.strip().lower() for s in settings.skip_folders}
    for folder, lot_type in settings.lot_types.items():
        base = settings.root / folder
        try:
            subs = sorted(
                (p for p in base.iterdir() if p.is_dir() and p.name.strip().lower() not in skip),
                key=lambda p: natural_key(p.name),
            )
        except OSError:
            continue
        found.extend((lot_type, p) for p in subs)
    return found


@dataclass(frozen=True)
class LotInfo:
    lot_type: str
    project: str
    part: str
    lot: str


def lots(settings: Settings, chosen: list[tuple[str, Path]]) -> list[LotInfo]:
    """Every lot with an AOI folder in the chosen projects (for picking lots in the window)."""

    def one(job: tuple[str, Path]) -> list[LotInfo]:
        lot_type, project = job
        found = find_lots(project, settings.machine_folder, settings.skip_folders)
        return [LotInfo(lot_type, project.name, part, lot) for part, lot in found]

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        result = [info for infos in pool.map(one, chosen) for info in infos]
    return sorted(result, key=lambda i: (natural_key(i.lot), i.lot_type, i.project))


def _scan(
    settings: Settings, options: RunOptions, progress: Progress
) -> tuple[list[FoundFile], list[IssueRow]]:
    scope = options.scope
    jobs = [(t, p) for t, p in projects(settings) if scope.has_project(t, p.name)]
    if not jobs:
        raise RunError(f"No project below {settings.root} fits the run ({scope.describe()}).")
    if scope.projects is not None:
        known = {f"{t}/{p.name}".lower() for t, p in jobs}
        missing = sorted(scope.projects - known)
        if missing:
            progress(f"Not found (skipped): {', '.join(missing)}")
    issues: list[IssueRow] = []
    for folder, lot_type in settings.lot_types.items():
        if not (settings.root / folder).is_dir() and scope.has_lot_type(lot_type):
            message = f"Lot type folder not found: {settings.root / folder}"
            issues.append(IssueRow("ERROR", "missing_folder", message, lot_type=lot_type))
    progress(f"Looking for AOI folders in {len(jobs)} project(s) ...")
    found: list[FoundFile] = []

    def one(job: tuple[str, Path]) -> tuple[list[FoundFile], list[IssueRow]]:
        lot_type, project = job
        return scan_project(project, lot_type, settings.root, settings.machine_folder,
                            settings.skip_folders, scope.lots)  # fmt: skip

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        for files, problems in pool.map(one, jobs):
            found.extend(files)
            issues.extend(problems)
    return found, issues


# -- reading ---------------------------------------------------------------------------------


def _update_cache(
    settings: Settings,
    found: list[FoundFile],
    cache: dict[str, dict[str, Any]],
    options: RunOptions,
    progress: Progress,
    stamp: datetime,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """The cache for the files found now; new and changed files are read. Files outside
    the run's scope keep their cache entries (their lots were not looked at)."""
    full_reload = options.full_reload
    updated = {k: v for k, v in cache.items() if not _in_scope(v, options.scope)}
    in_scope = set(cache) - set(updated)
    to_read: list[tuple[FoundFile, int, int]] = []
    stats = {"found": len(found), "read": 0, "unchanged": 0, "failed": 0, "removed": 0}
    for item in found:
        key = str(item.path)
        stat = _stat(item.path)
        if stat is None:
            continue
        old = cache.get(key)
        same = old and old.get("size") == stat[0] and old.get("mtime") == stat[1]
        if same and not full_reload and not old.get("failed"):
            old["side"] = list(item.side.key())  # settings (lot type names) may have changed
            old["folder"] = str(item.side.folder)
            updated[key] = old
            stats["unchanged"] += 1
        else:
            to_read.append((item, *stat))
    stats["removed"] = len(in_scope - {str(f.path) for f in found})
    if to_read:
        progress(f"Reading {len(to_read)} new or changed workbook(s) ...")

    def read(job: tuple[FoundFile, int, int]) -> tuple[str, dict[str, Any]]:
        item, size, mtime = job
        result = read_file(item, settings.defects_sheet)
        return str(item.path), _entry(item, size, mtime, result, stamp)

    done = 0
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        for key, entry in pool.map(read, to_read):
            updated[key] = entry
            done += 1
            stats["read"] += 1
            stats["failed"] += int(entry["failed"])
            if done % 200 == 0:
                progress(f"  {done} / {len(to_read)} read")
    return updated, stats


# -- tables ----------------------------------------------------------------------------------


def _side_row(entry: dict[str, Any]) -> dict[str, Any]:
    return dict(zip(SIDE_KEY, entry["side"], strict=True))


def _lot_key(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series([], index=frame.index, dtype=object)
    return (frame["Lot Type"].astype(str) + "|" + frame["Lot Number"].astype(str)).astype(object)


def _frame(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=list(columns))
    for column in ("Measured At", "Loaded At", "Found At", "Run At", "Last Measured"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in ("Count", "Defects", "Files", "Total Defects"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype("int64")
    return frame


def _sort(frame: pd.DataFrame, extra: tuple[str, ...] = ()) -> pd.DataFrame:
    if frame.empty:
        return frame.reset_index(drop=True)
    columns = [c for c in (*SIDE_KEY, *extra) if c in frame.columns]
    return frame.sort_values(columns, key=lambda s: s.map(lambda v: sort_text(str(v)))).reset_index(
        drop=True
    )


def code_order(codes: list[str]) -> list[str]:
    """Codes in natural order, 'Not verified' last."""
    return sorted((c for c in codes if c != NOT_VERIFIED), key=natural_key) + (
        [NOT_VERIFIED] if NOT_VERIFIED in codes else []
    )


def build_tables(
    cache: dict[str, dict[str, Any]],
    scan_issues: list[IssueRow],
    stamp: datetime,
    lot_names: dict[tuple[str, str], str] | None = None,
) -> dict[str, pd.DataFrame]:
    data: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    def add_issue(row: IssueRow) -> None:
        issues.append({
            "Severity": row.severity, "Category": row.category, "Lot Type": row.lot_type,
            "Project": row.project, "Message": row.message, "Source File": row.source_file,
            "Path": row.path, "Found At": stamp,
        })  # fmt: skip

    for row in scan_issues:
        add_issue(row)
    by_side: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for path, entry in cache.items():
        side = _side_row(entry)
        common = {**side, "Source File": entry["file"], "Path": path,
                  "Measured At": entry["measured_at"], "Loaded At": entry["loaded_at"]}  # fmt: skip
        for severity, category, message, source_file, lot_type, project, lot in entry["issues"]:
            add_issue(
                IssueRow(severity, category, message, source_file, path, lot_type, project, lot)
            )
        files.append({**common, "Defects": sum(entry["counts"].values()),
                      "Read OK": not entry["failed"]})  # fmt: skip
        if entry["failed"]:
            continue
        by_side.setdefault(tuple(entry["side"]), []).append({**entry, "path": path})
        for verification, count in entry["counts"].items():
            data.append({**common, "Verification": verification, "Count": count})

    # The AOI splits a scan every 500 defects; files of one side folder are added up.
    # The same DEFECT_ID in two files may be a re-scan, which would count twice.
    for entries in by_side.values():
        if len(entries) < 2:
            continue
        seen: dict[str, str] = {}
        overlaps: dict[tuple[str, str], int] = {}
        for entry in sorted(entries, key=lambda e: natural_key(e["file"])):
            for ident in set(entry["ids"]):
                first = seen.setdefault(ident, entry["file"])
                if first != entry["file"]:
                    pair = (first, entry["file"])
                    overlaps[pair] = overlaps.get(pair, 0) + 1
        for (first, second), count in overlaps.items():
            side = entries[0]
            add_issue(IssueRow(
                "WARNING", "possible_rescan",
                f"{count} DEFECT_ID(s) appear in both '{first}' and '{second}'. If one is a "
                f"re-scan, this panel side is counted twice.",
                second, side["folder"], side["side"][0], side["side"][1],
            ))  # fmt: skip

    data_frame = _frame(data, DATA_COLUMNS)
    data_frame[LOT_KEY] = _lot_key(data_frame)
    files_frame = _frame(files, FILE_COLUMNS)
    files_frame[LOT_KEY] = _lot_key(files_frame)
    issue_frame = _frame(issues, ISSUE_COLUMNS)
    order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    if not issue_frame.empty:
        issue_frame = issue_frame.sort_values(
            ["Severity", "Lot Type", "Project", "Path"],
            key=lambda s: s.map(order) if s.name == "Severity" else s.astype(str),
        ).reset_index(drop=True)
    return {
        DATA: _sort(data_frame, ("Source File", "Verification")),
        FILES: _sort(files_frame, ("Source File",)),
        ISSUES: issue_frame,
        WIDE: build_wide(files_frame, data_frame, lot_names or {}),
    }


def build_wide(
    files: pd.DataFrame, data: pd.DataFrame, lot_names: dict[tuple[str, str], str]
) -> pd.DataFrame:
    """One row per panel side: the files of its folder added up, a column per code.
    ``Lot Name`` is empty unless it was filled in the last AOI_Wide.xlsx."""
    keys = list(SIDE_KEY)
    ok = files[files["Read OK"]] if not files.empty else files
    if ok.empty:
        return pd.DataFrame(columns=list(WIDE_FIXED))
    ok = ok.assign(Folder=ok["Path"].map(lambda p: str(Path(p).parent)))
    sides = ok.groupby(keys, sort=False).agg(
        **{
            "Files": ("Source File", "count"),
            "Total Defects": ("Defects", "sum"),
            "Last Measured": ("Measured At", "max"),
            "Folder": ("Folder", "first"),
        }
    )
    codes = code_order(sorted(data["Verification"].unique())) if not data.empty else []
    if codes:
        pivot = data.pivot_table(
            index=keys, columns="Verification", values="Count", aggfunc="sum", fill_value=0
        )
        sides = sides.join(pivot[codes], how="left")
        sides[codes] = sides[codes].fillna(0).astype("int64")
    wide = sides.reset_index()
    wide[LOT_NAME] = [
        lot_names.get((str(t), str(n)), "")
        for t, n in zip(wide["Lot Type"], wide["Lot Number"], strict=True)
    ]
    return _sort(wide[[*WIDE_FIXED, *codes]])


def _cell_text(value: object) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def read_lot_names(path: Path) -> dict[tuple[str, str], str]:
    """(Lot Type, Lot Number) -> Lot Name, as filled in on the Wide sheet of ``path``."""
    if not path.is_file():
        return {}
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception:  # an unreadable old file must not stop the run
        logger.warning("Lot names cannot be read from %s", path)
        return {}
    names: dict[tuple[str, str], str] = {}
    try:
        if "Wide" not in workbook.sheetnames:
            return {}
        rows = workbook["Wide"].iter_rows(values_only=True)
        header = [_cell_text(h) for h in next(rows, ())]
        if not {"Lot Type", "Lot Number", LOT_NAME} <= set(header):
            return {}
        at = {name: header.index(name) for name in ("Lot Type", "Lot Number", LOT_NAME)}
        for row in rows:
            cells = [_cell_text(row[at[n]]) if at[n] < len(row) else "" for n in at]
            lot_type, lot, name = cells
            if lot_type and lot and name:
                names.setdefault((lot_type, lot), name)
    finally:
        workbook.close()
    return names


# -- publishing ------------------------------------------------------------------------------


def _load_runs(output: Path) -> pd.DataFrame:
    path = output / f"{RUNS}.parquet"
    if path.is_file():
        try:
            return pd.read_parquet(path)
        except (OSError, ValueError):
            logger.warning("Run history %s cannot be read; starting a new one.", path)
    return pd.DataFrame(columns=list(RUN_COLUMNS))


def _write_excel(path: Path, tables: dict[str, pd.DataFrame], stamp: datetime) -> Path:
    """AOI_Wide.xlsx; when Excel has it open, a copy with the time in its name."""
    try:
        return _excel(path, tables)
    except PermissionError:
        other = path.with_name(f"{path.stem}_{stamp:%Y%m%d_%H%M%S}{path.suffix}")
        logger.warning("%s is open in Excel; written as %s", path.name, other.name)
        return _excel(other, tables)


def _excel(path: Path, tables: dict[str, pd.DataFrame]) -> Path:
    temporary = path.with_name(f"~tmp_{path.name}")
    with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
        for sheet, frame in (("Wide", tables[WIDE]), ("Files", tables[FILES]),
                             ("Issues", tables[ISSUES])):  # fmt: skip
            frame.to_excel(writer, sheet_name=sheet, index=False)
            ws = writer.sheets[sheet]
            ws.freeze_panes = "A2"
            if ws.max_row > 1:
                ws.auto_filter.ref = ws.dimensions
            for column_cells in ws.iter_cols(min_row=1, max_row=min(ws.max_row, 200)):
                width = max(len(str(c.value)) if c.value is not None else 0 for c in column_cells)
                ws.column_dimensions[column_cells[0].column_letter].width = min(
                    max(width + 2, 8), 60
                )
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    if isinstance(cell.value, datetime):
                        cell.number_format = "yyyy-mm-dd hh:mm"
    temporary.replace(path)
    return path


def publish(
    output: Path, tables: dict[str, pd.DataFrame], runs: pd.DataFrame, stamp: datetime
) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in (DATA, FILES, ISSUES):
        path = output / f"{name}.parquet"
        tables[name].to_parquet(path, index=False)
        written.append(path)
    path = output / f"{RUNS}.parquet"
    runs.to_parquet(path, index=False)
    written.append(path)
    written.append(_write_excel(output / f"{WIDE}.xlsx", tables, stamp))
    return written


# -- run ---------------------------------------------------------------------------------------


def _quiet(_: str) -> None:
    pass


def run(settings: Settings, options: RunOptions, progress: Progress = _quiet) -> RunReport:
    start = time.monotonic()
    stamp = datetime.now().replace(microsecond=0)
    output, cache_dir = places(settings, options)
    if not settings.root.is_dir():
        raise RunError(f"Source folder not found: {settings.root}")
    with RunLock(cache_dir):
        cache, old_issues = _load_cache(cache_dir)
        progress(f"Run for {options.scope.describe()}")
        found, scan_issues = _scan(settings, options, progress)
        progress(f"{len(found)} AOI workbook(s) found.")
        cache, stats = _update_cache(settings, found, cache, options, progress, stamp)
        kept = [i for i in old_issues if not options.scope.contains(i.lot_type, i.project, i.lot)]
        scan_issues = kept + scan_issues
        _save_cache(cache_dir, cache, scan_issues)
        lot_names = read_lot_names(output / f"{WIDE}.xlsx")
        tables = build_tables(cache, scan_issues, stamp, lot_names)
        record = {
            "Run At": stamp, "Mode": options.mode, "Version": __version__,
            "Result": "OK" if stats["failed"] == 0 else "OK with failed files",
            "Files Found": stats["found"], "Files Read": stats["read"],
            "Files Unchanged": stats["unchanged"], "Files Removed": stats["removed"],
            "Files Failed": stats["failed"], "Side Folders": len(tables[WIDE]),
            "Defects": int(tables[DATA]["Count"].sum()) if not tables[DATA].empty else 0,
            "Issues": len(tables[ISSUES]), "Seconds": round(time.monotonic() - start, 1),
        }  # fmt: skip
        runs = _load_runs(output)
        new = pd.DataFrame([record], columns=list(RUN_COLUMNS))
        runs = new if runs.empty else pd.concat([runs, new], ignore_index=True)
        runs["Run At"] = pd.to_datetime(runs["Run At"], errors="coerce")
        progress(f"Writing the tables to {output} ...")
        try:
            written = publish(output, tables, runs, stamp)
        except OSError as exc:
            raise RunError(f"Tables cannot be written to {output}: {exc}") from exc
    return RunReport(run=record, output=output, written=written)
