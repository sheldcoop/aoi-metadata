"""The small records passed between scanning, reading and publishing.

They follow ``hrm_converter.dashboard.sources.base`` of the HRM + PRF dashboard
(``FoundFile``, ``IssueRow``, ``FileRead``), so this tool can become a source
of that dashboard without changing how files are found and read.
"""

from __future__ import annotations

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
