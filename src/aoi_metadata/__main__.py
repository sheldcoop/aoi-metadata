"""Start the AOI export.

python -m aoi_metadata                       the window
python -m aoi_metadata run                   one run, no window (Task Scheduler)
python -m aoi_metadata run --full-reload     read every workbook again
python -m aoi_metadata run --lot-type Shippable
python -m aoi_metadata run --project "Shippable/Chiplet4Future" --lot 19621,19622
python -m aoi_metadata run --project "Engineering/ProjX" --test    (to a test folder)

Options can be repeated or comma separated. A run with --lot-type, --project or
--lot only updates those lots; every other lot keeps its rows in the output.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aoi_metadata import __version__, engine
from aoi_metadata.models import Scope
from aoi_metadata.settings import SETTINGS_FILE, SettingsError, load

PROJECT_DIR = Path(__file__).resolve().parents[2]


def setup_logging(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "aoi_metadata.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8")],
        force=True,
    )
    return path


def _split(values: list[str] | None) -> list[str]:
    """['a, b', 'c'] -> ['a', 'b', 'c']."""
    return [part.strip() for v in values or [] for part in v.split(",") if part.strip()]


def parse_scope(args: argparse.Namespace) -> Scope:
    projects = _split(args.project)
    for project in projects:
        lot_type, _, name = project.partition("/")
        if not lot_type.strip() or not name.strip():
            raise ValueError(
                f"--project needs 'Lot type/Project' (e.g. Shippable/Chiplet4Future), "
                f"not {project!r}"
            )
    return Scope.make(_split(args.lot_type), projects, _split(args.lot))


def _run(args: argparse.Namespace) -> int:
    try:
        settings = load(args.settings)
        scope = parse_scope(args)
    except (SettingsError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    options = engine.RunOptions(full_reload=args.full_reload, scope=scope, test=args.test)
    try:
        log = setup_logging(settings.cache / "logs")
    except OSError:
        log = Path("(no log file)")
    print(f"AOI metadata export {__version__} ({options.mode})", flush=True)
    try:
        report = engine.run(settings, options, progress=lambda line: print(line, flush=True))
    except engine.RunError as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        print(f"Log: {log}")
        return 1
    print("Finished")
    for name in ("Files Found", "Files Read", "Files Unchanged", "Files Removed", "Files Failed",
                 "Side Folders", "Defects", "Issues", "Seconds"):  # fmt: skip
        print(f"  {name:<16} {report.run[name]}")
    print(f"Output: {report.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aoi_metadata",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=PROJECT_DIR / SETTINGS_FILE,
        help=f"settings file (default: {SETTINGS_FILE} next to AOI.bat)",
    )
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="one run without a window")
    run.add_argument("--full-reload", action="store_true", help="read every workbook again")
    run.add_argument("--lot-type", action="append", metavar="NAME",
                     help="only this lot type, e.g. Shippable or Engineering")  # fmt: skip
    run.add_argument("--project", action="append", metavar="LOTTYPE/PROJECT",
                     help="only this project, e.g. Shippable/Chiplet4Future")  # fmt: skip
    run.add_argument("--lot", action="append", metavar="LOT", help="only this lot, e.g. 19621")
    run.add_argument("--test", action="store_true",
                     help="write to a test folder on this PC, not the output folder")  # fmt: skip
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    from aoi_metadata.gui import start

    return start(args.settings)


if __name__ == "__main__":
    sys.exit(main())
