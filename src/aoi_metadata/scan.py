"""Find the AOI workbooks of one project folder.

The layout (the AOI machine folder holds the buildups, like HRM can)::

    <Lot type>\\<Project>\\<Part>\\<Lot>\\AOI\\<BU-01>\\<Process>\\Panel <id>\\<Front|Back>\\*.xlsx

Every side folder may hold several workbooks: the AOI writes at most 500
defects per file and starts a new one after that. Anything that does not fit
the layout is reported in Issues instead of being skipped silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from aoi_metadata.models import FoundFile, IssueRow, SideFolder

EXCEL_SUFFIXES = (".xlsx", ".xlsm")
LEGACY_SUFFIXES = (".xls",)
LAYOUT = r"<Lot>\AOI\<BU-01>\<Process>\Panel <id>\<Front|Back>\*.xlsx"

BUILDUP_RE = re.compile(r"^BU[\s_-]*0*(\d{1,3})$", re.IGNORECASE)
PANEL_RE = re.compile(r"^Panel[\s_-]*(\S.*)$", re.IGNORECASE)
SIDES = {"front": "Front", "back": "Back", "f": "Front", "b": "Back"}
# How deep below the project folder the AOI folder may be (<Part>\<Lot>\AOI = 3).
MAX_AOI_DEPTH = 3


def natural_key(text: str) -> list[object]:
    """'BU-2' before 'BU-10', 'Panel 9' before 'Panel 10'."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def sort_text(text: str) -> str:
    """natural_key as one string (pandas sorts by it): numbers padded to 12 digits."""
    return re.sub(r"\d+", lambda m: m.group(0).zfill(12), text.lower())


def buildup_name(folder: str) -> str | None:
    """'BU-01', 'BU01', 'bu_1' -> 'BU-01'; anything else -> None."""
    match = BUILDUP_RE.match(folder.strip())
    return f"BU-{int(match.group(1)):02d}" if match else None


def panel_name(folder: str) -> str | None:
    """'Panel 3202' -> '3202'; anything else -> None."""
    match = PANEL_RE.match(folder.strip())
    return match.group(1).strip() if match else None


def side_name(folder: str) -> str | None:
    return SIDES.get(folder.strip().lower())


def _dirs(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: natural_key(p.name),
    )


def _excel_files(folder: Path) -> list[Path]:
    return sorted(
        (
            p
            for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() in EXCEL_SUFFIXES + LEGACY_SUFFIXES
            and not p.name.startswith("~$")
        ),
        key=lambda p: natural_key(p.name),
    )


class Scanner:
    """Walks one project folder; collects the workbooks and the problems found."""

    def __init__(self, machine_folder: str, skip_folders: list[str]) -> None:
        self.machine = machine_folder.strip().lower()
        self.skip = {s.strip().lower() for s in skip_folders}
        self.root: Path = Path()
        self.lot_type = ""
        self.project = ""
        self.lot = ""
        self.lots: frozenset[str] | None = None  # only these lot folders (lower case)
        self.issues: list[IssueRow] = []

    # -- helpers ---------------------------------------------------------------------

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _issue(
        self, severity: str, category: str, message: str, path: Path, file: str = ""
    ) -> None:
        self.issues.append(
            IssueRow(severity, category, message, file, self._relative(path), self.lot_type,
                     self.project, self.lot)
        )  # fmt: skip

    def _listing(self, folder: Path) -> tuple[list[Path], list[Path]] | None:
        try:
            return _dirs(folder), _excel_files(folder)
        except OSError as exc:
            self._issue("ERROR", "unreadable_folder", f"Folder cannot be listed: {exc}", folder)
            return None

    def _misplaced(self, folder: Path, files: list[Path], level: str) -> None:
        if files:
            names = ", ".join(f.name for f in files)
            self._issue(
                "WARNING",
                "misplaced_workbook",
                f"{len(files)} Excel file(s) lie directly in the {level} folder and were not "
                f"read: {names}. AOI workbooks are only read from side folders ({LAYOUT}).",
                folder,
            )

    def _skipped(self, folder: Path) -> bool:
        return folder.name.strip().lower() in self.skip

    # -- walking ---------------------------------------------------------------------

    def scan(self, project_dir: Path, lot_type: str, root: Path) -> list[FoundFile]:
        """Every AOI workbook below ``project_dir``."""
        self.root, self.lot_type, self.project = root, lot_type, project_dir.name
        found: list[FoundFile] = []
        for aoi_dir, between in self._aoi_folders(project_dir):
            lot = aoi_dir.parent.name
            part = between[0] if len(between) >= 2 else ""
            self.lot = lot
            found.extend(self._aoi(aoi_dir, part, lot))
            self.lot = ""
        return found

    def _aoi_folders(self, project_dir: Path) -> list[tuple[Path, list[str]]]:
        """(AOI folder, folder names between project and AOI) up to MAX_AOI_DEPTH deep."""
        result: list[tuple[Path, list[str]]] = []
        level: list[tuple[Path, list[str]]] = [(project_dir, [])]
        for _ in range(MAX_AOI_DEPTH):
            following: list[tuple[Path, list[str]]] = []
            for folder, names in level:
                listing = self._listing(folder)
                if listing is None:
                    continue
                for sub in listing[0]:
                    if self._skipped(sub):
                        continue
                    if sub.name.strip().lower() == self.machine:
                        if names and self.lots is not None:
                            if names[-1].strip().lower() in self.lots:
                                result.append((sub, names))
                        elif names:  # an AOI folder directly in the project has no lot
                            result.append((sub, names))
                        else:
                            self._issue(
                                "WARNING", "folder_layout",
                                f"'{sub.name}' folder directly in the project has no lot "
                                f"folder above it; not read. Expected {LAYOUT}.", sub,
                            )  # fmt: skip
                    else:
                        following.append((sub, [*names, sub.name]))
            level = following
        return result

    def _aoi(self, aoi_dir: Path, part: str, lot: str) -> list[FoundFile]:
        listing = self._listing(aoi_dir)
        if listing is None:
            return []
        dirs, files = listing
        self._misplaced(aoi_dir, files, self.machine.upper())
        found: list[FoundFile] = []
        for bu_dir in dirs:
            buildup = buildup_name(bu_dir.name)
            if buildup is None:
                if not self._skipped(bu_dir):
                    self._issue(
                        "WARNING", "folder_layout",
                        f"Folder '{bu_dir.name}' in the AOI folder is not a buildup "
                        f"(BU-01, BU02, ...); not read.", bu_dir,
                    )  # fmt: skip
                continue
            found.extend(self._buildup(bu_dir, part, lot, buildup))
        if not dirs and not files:
            self._issue("INFO", "empty_folder", "AOI folder is empty.", aoi_dir)
        return found

    def _buildup(self, bu_dir: Path, part: str, lot: str, buildup: str) -> list[FoundFile]:
        listing = self._listing(bu_dir)
        if listing is None:
            return []
        dirs, files = listing
        self._misplaced(bu_dir, files, "buildup")
        found: list[FoundFile] = []
        for process_dir in dirs:
            if self._skipped(process_dir):
                continue
            if panel_name(process_dir.name) is not None:
                self._issue(
                    "WARNING", "folder_layout",
                    f"Panel folder '{process_dir.name}' lies directly in the buildup folder; "
                    f"a process folder (e.g. 'Post FET') is expected in between. Not read.",
                    process_dir,
                )  # fmt: skip
                continue
            found.extend(self._process(process_dir, part, lot, buildup))
        return found

    def _process(self, process_dir: Path, part: str, lot: str, buildup: str) -> list[FoundFile]:
        listing = self._listing(process_dir)
        if listing is None:
            return []
        dirs, files = listing
        self._misplaced(process_dir, files, "process")
        found: list[FoundFile] = []
        for panel_dir in dirs:
            panel = panel_name(panel_dir.name)
            if panel is None:
                if not self._skipped(panel_dir):
                    self._issue(
                        "WARNING", "folder_layout",
                        f"Folder '{panel_dir.name}' in the process folder is not a panel "
                        f"('Panel <id>'); not read.", panel_dir,
                    )  # fmt: skip
                continue
            found.extend(self._panel(panel_dir, part, lot, buildup, process_dir.name, panel))
        return found

    def _panel(
        self, panel_dir: Path, part: str, lot: str, buildup: str, process: str, panel: str
    ) -> list[FoundFile]:
        listing = self._listing(panel_dir)
        if listing is None:
            return []
        dirs, files = listing
        self._misplaced(panel_dir, files, "panel")
        found: list[FoundFile] = []
        for side_dir in dirs:
            side = side_name(side_dir.name)
            if side is None:
                if not self._skipped(side_dir):
                    self._issue(
                        "WARNING", "folder_layout",
                        f"Folder '{side_dir.name}' in the panel folder is not 'Front' or "
                        f"'Back'; not read.", side_dir,
                    )  # fmt: skip
                continue
            folder = SideFolder(
                self.lot_type, self.project, part, lot, buildup, process, panel, side, side_dir
            )
            found.extend(self._side(folder))
        return found

    def _side(self, side: SideFolder) -> list[FoundFile]:
        listing = self._listing(side.folder)
        if listing is None:
            return []
        dirs, files = listing
        for sub in dirs:
            try:
                nested = sum(
                    1 for p in sub.rglob("*")
                    if p.is_file() and p.suffix.lower() in EXCEL_SUFFIXES + LEGACY_SUFFIXES
                )  # fmt: skip
            except OSError:
                nested = 0
            if nested:
                self._issue(
                    "WARNING", "misplaced_workbook",
                    f"Sub-folder '{sub.name}' of this side folder holds {nested} Excel "
                    f"file(s); sub-folders of a side folder are not read.", side.folder,
                )  # fmt: skip
        found: list[FoundFile] = []
        for file in files:
            if file.suffix.lower() in LEGACY_SUFFIXES:
                self._issue(
                    "WARNING", "legacy_xls",
                    "Old .xls workbook is not read; save it as .xlsx.", side.folder, file.name,
                )  # fmt: skip
                continue
            found.append(FoundFile(file, side))
        if not found:
            self._issue("INFO", "no_workbook", "No AOI workbook in this side folder.", side.folder)
        return found


def scan_project(
    project_dir: Path,
    lot_type: str,
    root: Path,
    machine_folder: str,
    skip_folders: list[str],
    lots: frozenset[str] | None = None,
) -> tuple[list[FoundFile], list[IssueRow]]:
    scanner = Scanner(machine_folder, skip_folders)
    scanner.lots = lots
    found = scanner.scan(project_dir, lot_type, root)
    return found, scanner.issues
