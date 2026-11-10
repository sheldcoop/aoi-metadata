"""The small records passed between scanning, reading and publishing.

They follow ``hrm_converter.dashboard.sources.base`` of the HRM + PRF dashboard
(``FoundFile``, ``IssueRow``, ``FileRead``), so this tool can become a source
of that dashboard without changing how files are found and read.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

NOT_VERIFIED = "Not verified"

# Where a panel side sits in the folder tree; one row of the wide export.
SIDE_KEY = (
    "Lot Type",
    "Project",
    "Part Number",
    "Lot Number",
    "Buildup",
    "Process",
    "Panel",
    "Side",
)


@dataclass(frozen=True)
class SideFolder:
    """``...\\<Lot>\\AOI\\BU-01\\Post FET\\Panel 3202\\Back`` and what its path says."""

    lot_type: str
    project: str
    part: str
    lot: str
    buildup: str  # always 'BU-01'
    process: str
    panel: str  # '3202'
    side: str  # 'Front' / 'Back'
    folder: Path

    def key(self) -> tuple[str, ...]:
        return (
            self.lot_type,
            self.project,
            self.part,
            self.lot,
            self.buildup,
            self.process,
            self.panel,
            self.side,
        )


@dataclass(frozen=True)
class FoundFile:
    """One AOI workbook found in a side folder."""

    path: Path
    side: SideFolder


@dataclass
class IssueRow:
    severity: str  # ERROR / WARNING / INFO
    category: str
    message: str
    source_file: str = ""
    path: str = ""
    lot_type: str = ""
    project: str = ""
    lot: str = ""


def _folded(values: Iterable[str] | None) -> frozenset[str] | None:
    if values is None:
        return None
    return frozenset(str(v).strip().lower() for v in values if str(v).strip())


@dataclass(frozen=True)
class Scope:
    """Which lots a run looks at; ``None`` means all. Lots outside it keep their old rows."""

    lot_types: frozenset[str] | None = None
    projects: frozenset[str] | None = None  # 'lot type/project'
    lots: frozenset[str] | None = None

    @classmethod
    def make(
        cls,
        lot_types: Iterable[str] | None = None,
        projects: Iterable[str] | None = None,
        lots: Iterable[str] | None = None,
    ) -> Scope:
        """Case and spaces do not matter; an empty list means all."""
        folded = [_folded(v) for v in (lot_types, projects, lots)]
        return cls(*(f or None for f in folded))

    @property
    def everything(self) -> bool:
        return self.lot_types is None and self.projects is None and self.lots is None

    def has_lot_type(self, lot_type: str) -> bool:
        return self.lot_types is None or lot_type.strip().lower() in self.lot_types

    def has_project(self, lot_type: str, project: str) -> bool:
        key = f"{lot_type.strip()}/{project.strip()}".lower()
        return self.has_lot_type(lot_type) and (self.projects is None or key in self.projects)

    def has_lot(self, lot: str) -> bool:
        return self.lots is None or lot.strip().lower() in self.lots

    def contains(self, lot_type: str, project: str, lot: str) -> bool:
        """A row or issue of this lot belongs to the run (no lot: its project decides)."""
        if not lot_type:
            return self.everything
        if not project:
            return self.has_lot_type(lot_type)
        return self.has_project(lot_type, project) and (not lot or self.has_lot(lot))

    def describe(self) -> str:
        if self.everything:
            return "all lots"
        parts = []
        for name, values in (("lot types", self.lot_types), ("projects", self.projects),
                             ("lots", self.lots)):  # fmt: skip
            if values is not None:
                parts.append(f"{name}: {', '.join(sorted(values))}")
        return "; ".join(parts)


@dataclass
class FileRead:
    """What one workbook holds: defects per Verification code, and its defect ids."""

    counts: dict[str, int] = field(default_factory=dict)
    defect_ids: list[str] = field(default_factory=list)
    issues: list[IssueRow] = field(default_factory=list)
    measured_at: datetime | None = None
    failed: bool = False  # nothing usable: the file is tried again on the next run

    @property
    def total(self) -> int:
        return sum(self.counts.values())
