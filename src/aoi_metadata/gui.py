"""The window: folders, what to run (lot types, projects, lots), schedule, and the run log."""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from aoi_metadata import __version__, engine
from aoi_metadata.models import Scope
from aoi_metadata.settings import Settings, SettingsError, load, save

TASK_NAME = "AOI Metadata Export"
TILE = QSize(116, 44)  # one lot tile


def bat_path() -> Path:
    """AOI.bat in the project folder (two levels above the package)."""
    return Path(__file__).resolve().parents[2] / "AOI.bat"


def schedule_command(hours: int, start: str = "07:00") -> list[str]:
    """schtasks arguments: every ``hours`` hours, as you, only while you are logged on."""
    action = f'"{bat_path()}" scheduled'
    return ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", str(hours),
            "/ST", start, "/TR", action]  # fmt: skip


def run_schtasks(arguments: list[str]) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Scheduling works on Windows only."
    try:
        done = subprocess.run(  # noqa: S603
            arguments, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout or done.stderr).strip()


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607


class Worker(QObject):
    line = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, settings: Settings, options: engine.RunOptions) -> None:
        super().__init__()
        self.settings, self.options = settings, options

    def run(self) -> None:
        try:
            report = engine.run(self.settings, self.options, progress=self.line.emit)
        except engine.RunError as exc:
            self.finished.emit(False, str(exc))
            return
        except Exception:  # shown in the log instead of closing the window
            self.finished.emit(False, traceback.format_exc())
            return
        summary = ", ".join(
            f"{k}: {report.run[k]}"
            for k in (
                "Files Found",
                "Files Read",
                "Files Failed",
                "Side Folders",
                "Defects",
                "Issues",
                "Seconds",
            )  # fmt: skip
        )
        self.finished.emit(True, f"{summary}\nOutput: {report.output}")


class Window(QWidget):
    def __init__(self, settings_path: Path) -> None:
        super().__init__()
        self.settings_path = settings_path
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.last_output: Path | None = None
        self.setWindowTitle(f"AOI Metadata Export {__version__}")
        self.resize(820, 640)
        try:
            self.settings = load(settings_path)
        except SettingsError as exc:
            QMessageBox.warning(self, "Settings", f"{exc}\n\nThe defaults are used.")
            self.settings = Settings()

        layout = QVBoxLayout(self)

        folders = QGroupBox("Folders")
        form = QFormLayout(folders)
        self.source = QLineEdit(self.settings.source_root)
        self.output = QLineEdit(self.settings.output_dir)
        form.addRow("Measurement results", self._with_browse(self.source))
        form.addRow("Output folder", self._with_browse(self.output))
        save_button = QPushButton("Save folders")
        save_button.clicked.connect(self.save_settings)
        form.addRow("", save_button)
        layout.addWidget(folders)

        scope = QGroupBox("What to run")
        scope_layout = QVBoxLayout(scope)
        type_row = QHBoxLayout()
        self.lot_type_boxes: dict[str, QCheckBox] = {}
        for folder, lot_type in self.settings.lot_types.items():
            box = QCheckBox(f"{folder}  ({lot_type})")
            box.setChecked(True)
            box.toggled.connect(self.filter_projects)
            self.lot_type_boxes[lot_type] = box
            type_row.addWidget(box)
        type_row.addStretch()
        scope_layout.addLayout(type_row)
        project_row = QHBoxLayout()
        project_row.addWidget(QLabel("Projects:"))
        self.project_filter = QLineEdit()
        self.project_filter.setPlaceholderText("type to filter the list")
        self.project_filter.textChanged.connect(self.filter_projects)
        project_row.addWidget(self.project_filter, 1)
        load_button = QPushButton("Load projects")
        load_button.clicked.connect(self.load_projects)
        clear_button = QPushButton("Untick all")
        clear_button.clicked.connect(self.untick_projects)
        project_row.addWidget(load_button)
        project_row.addWidget(clear_button)
        scope_layout.addLayout(project_row)
        self.projects = QListWidget()
        self.projects.setMaximumHeight(130)
        self.projects.setToolTip("Tick projects to run only those. Nothing ticked: all projects.")
        scope_layout.addWidget(self.projects)
        lot_row = QHBoxLayout()
        lot_row.addWidget(QLabel("Lots:"))
        self.lot_filter = QLineEdit()
        self.lot_filter.setPlaceholderText("type to filter the tiles")
        self.lot_filter.textChanged.connect(self.filter_lots)
        lot_row.addWidget(self.lot_filter, 1)
        load_lots = QPushButton("Load lots")
        load_lots.setToolTip("Show the lots of the ticked projects (all projects if none ticked)")
        load_lots.clicked.connect(self.load_lots)
        all_lots = QPushButton("Select all")
        all_lots.clicked.connect(lambda: self._select_lots(True))
        no_lots = QPushButton("Clear")
        no_lots.clicked.connect(lambda: self._select_lots(False))
        for button in (load_lots, all_lots, no_lots):
            lot_row.addWidget(button)
        scope_layout.addLayout(lot_row)
        # Lots as tiles: click a tile to select it, click again to deselect.
        self.lot_tiles = QListWidget()
        self.lot_tiles.setViewMode(QListView.ViewMode.IconMode)
        self.lot_tiles.setFlow(QListView.Flow.LeftToRight)
        self.lot_tiles.setWrapping(True)
        self.lot_tiles.setResizeMode(QListView.ResizeMode.Adjust)
        self.lot_tiles.setMovement(QListView.Movement.Static)
        self.lot_tiles.setGridSize(QSize(TILE.width() + 8, TILE.height() + 8))
        self.lot_tiles.setUniformItemSizes(True)
        self.lot_tiles.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.lot_tiles.setMaximumHeight(160)
        self.lot_tiles.setStyleSheet(
            "QListWidget::item { border: 1px solid #b8b8b8; border-radius: 4px; }"
            "QListWidget::item:selected { background: #2f6fbf; color: white;"
            " border-color: #2f6fbf; }"
        )
        self.lot_tiles.itemSelectionChanged.connect(self._lot_count)
        scope_layout.addWidget(self.lot_tiles)
        self.lot_status = QLabel("No lot selected: all lots of the choice above.")
        scope_layout.addWidget(self.lot_status)
        scope_layout.addWidget(QLabel(
            "Nothing ticked or selected means everything. A run for some projects or lots "
            "updates only those; all other lots keep their rows in the output."
        ))  # fmt: skip
        row = QHBoxLayout()
        self.run_button = QPushButton("Run AOI")
        self.run_button.clicked.connect(lambda: self.start_scoped(test=False))
        self.test_button = QPushButton("Test run")
        self.test_button.setToolTip("Same choice, but written to a test folder on this PC")
        self.test_button.clicked.connect(lambda: self.start_scoped(test=True))
        self.full_button = QPushButton("Full reload")
        self.full_button.setToolTip("Read every workbook of the choice again (slow)")
        self.full_button.clicked.connect(self.full_reload)
        open_folder = QPushButton("Open output folder")
        open_folder.clicked.connect(self.open_output)
        open_excel = QPushButton("Open AOI_Wide.xlsx")
        open_excel.clicked.connect(self.open_excel)
        for button in (self.run_button, self.test_button, self.full_button):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(open_folder)
        row.addWidget(open_excel)
        scope_layout.addLayout(row)
        layout.addWidget(scope)

        schedule = QGroupBox("Schedule (Windows Task Scheduler)")
        schedule_row = QHBoxLayout(schedule)
        self.hours = QSpinBox()
        self.hours.setRange(1, 24)
        self.hours.setValue(self.settings.schedule_hours)
        self.hours.setSuffix(" h")
        create = QPushButton("Create / update")
        create.clicked.connect(self.schedule)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.unschedule)
        schedule_row.addWidget(QLabel("Run every"))
        schedule_row.addWidget(self.hours)
        schedule_row.addWidget(create)
        schedule_row.addWidget(remove)
        schedule_row.addStretch()
        layout.addWidget(schedule)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.say(f"Settings: {self.settings_path}")

    # -- helpers ---------------------------------------------------------------------

    def _with_browse(self, edit: QLineEdit) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit, 1)
        button = QPushButton("Browse ...")
        button.clicked.connect(lambda: self._browse(edit))
        row.addWidget(button)
        return box

    def _browse(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
        if folder:
            edit.setText(str(Path(folder)))

    def say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _current(self) -> Settings:
        self.settings.source_root = self.source.text().strip() or self.settings.source_root
        self.settings.output_dir = self.output.text().strip() or self.settings.output_dir
        self.settings.schedule_hours = self.hours.value()
        return self.settings

    def save_settings(self) -> None:
        try:
            save(self._current(), self.settings_path)
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Cannot save: {exc}")
            return
        self.say(f"Saved {self.settings_path}")

    # -- runs ------------------------------------------------------------------------

    def load_projects(self) -> None:
        ticked = set(self._ticked_projects())
        self.projects.clear()
        found = engine.projects(self._current())
        for lot_type, folder in found:
            name = f"{lot_type}/{folder.name}"
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, lot_type)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in ticked else Qt.CheckState.Unchecked)
            self.projects.addItem(item)
        self.filter_projects()
        self.say(f"{len(found)} project(s) found." if found else
                 f"No projects found below {self.settings.root}.")  # fmt: skip

    def filter_projects(self) -> None:
        text = self.project_filter.text().strip().lower()
        types = {t for t, box in self.lot_type_boxes.items() if box.isChecked()}
        for i in range(self.projects.count()):
            item = self.projects.item(i)
            visible = item.data(Qt.ItemDataRole.UserRole) in types and text in item.text().lower()
            item.setHidden(not visible)

    def untick_projects(self) -> None:
        for i in range(self.projects.count()):
            self.projects.item(i).setCheckState(Qt.CheckState.Unchecked)

    def _ticked_projects(self) -> list[str]:
        types = {t for t, box in self.lot_type_boxes.items() if box.isChecked()}
        return [
            item.text()
            for item in (self.projects.item(i) for i in range(self.projects.count()))
            if item.checkState() == Qt.CheckState.Checked
            and item.data(Qt.ItemDataRole.UserRole) in types
        ]

    def scope(self) -> Scope | None:
        """The choice in the window; None (with a message) when nothing can run."""
        types = [t for t, box in self.lot_type_boxes.items() if box.isChecked()]
        if not types:
            QMessageBox.information(self, "Run", "Tick at least one lot type.")
            return None
        lot_types = None if len(types) == len(self.lot_type_boxes) else types
        lots = sorted({item.data(Qt.ItemDataRole.UserRole) for item in self._selected_lots()})
        return Scope.make(lot_types, self._ticked_projects() or None, lots or None)

    # -- lot tiles -------------------------------------------------------------------

    def load_lots(self) -> None:
        settings = self._current()
        types = {t for t, box in self.lot_type_boxes.items() if box.isChecked()}
        ticked = {p.lower() for p in self._ticked_projects()}
        chosen = [
            (t, p) for t, p in engine.projects(settings)
            if t in types and (not ticked or f"{t}/{p.name}".lower() in ticked)
        ]  # fmt: skip
        keep = {item.data(Qt.ItemDataRole.UserRole) for item in self._selected_lots()}
        self.say(f"Looking for lots in {len(chosen)} project(s) ...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = engine.lots(settings, chosen)
        finally:
            QApplication.restoreOverrideCursor()
        self.lot_tiles.clear()
        for info in found:
            item = QListWidgetItem(f"{info.lot}\n{info.part or info.project}")
            item.setData(Qt.ItemDataRole.UserRole, info.lot)
            item.setToolTip(f"{info.lot_type} / {info.project} / {info.part} / {info.lot}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setSizeHint(TILE)
            self.lot_tiles.addItem(item)
            item.setSelected(info.lot in keep)
        self.filter_lots()
        self._lot_count()
        self.say(f"{len(found)} lot(s) with an AOI folder found.")

    def filter_lots(self) -> None:
        text = self.lot_filter.text().strip().lower()
        for i in range(self.lot_tiles.count()):
            item = self.lot_tiles.item(i)
            item.setHidden(bool(text) and text not in item.toolTip().lower())

    def _selected_lots(self) -> list[QListWidgetItem]:
        return self.lot_tiles.selectedItems()

    def _select_lots(self, selected: bool) -> None:
        for i in range(self.lot_tiles.count()):
            item = self.lot_tiles.item(i)
            if not item.isHidden() or not selected:
                item.setSelected(selected)

    def _lot_count(self) -> None:
        count = len(self._selected_lots())
        total = self.lot_tiles.count()
        self.lot_status.setText(
            f"{count} of {total} lot(s) selected." if count
            else "No lot selected: all lots of the choice above."
        )  # fmt: skip

    def start_scoped(self, test: bool, full_reload: bool = False) -> None:
        scope = self.scope()
        if scope is not None:
            self.start(engine.RunOptions(full_reload=full_reload, scope=scope, test=test))

    def full_reload(self) -> None:
        scope = self.scope()
        if scope is None:
            return
        answer = QMessageBox.question(
            self, "Full reload",
            f"Read every AOI workbook again ({scope.describe()})? This can take long.",
        )  # fmt: skip
        if answer == QMessageBox.StandardButton.Yes:
            self.start(engine.RunOptions(full_reload=True, scope=scope))

    def start(self, options: engine.RunOptions) -> None:
        if self.thread is not None:
            return
        settings = self._current()
        self.last_output = engine.places(settings, options)[0]
        self.say(f"\n--- Run ({options.mode}) ---")
        for button in (self.run_button, self.full_button, self.test_button):
            button.setEnabled(False)
        self.thread = QThread()
        self.worker = Worker(settings, options)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.line.connect(self.say)
        self.worker.finished.connect(self.done)
        self.thread.start()

    def done(self, ok: bool, message: str) -> None:
        self.say(("Finished. " if ok else "STOPPED: ") + message)
        assert self.thread is not None
        self.thread.quit()
        self.thread.wait()
        self.thread = self.worker = None
        for button in (self.run_button, self.full_button, self.test_button):
            button.setEnabled(True)

    def open_output(self) -> None:
        folder = self.last_output or self._current().output
        if folder.is_dir():
            open_path(folder)
        else:
            self.say(f"Output folder does not exist yet: {folder}")

    def open_excel(self) -> None:
        folder = self.last_output or self._current().output
        path = folder / f"{engine.WIDE}.xlsx"
        if path.is_file():
            open_path(path)
        else:
            self.say(f"Not written yet: {path}")

    # -- schedule --------------------------------------------------------------------

    def schedule(self) -> None:
        self.save_settings()
        ok, text = run_schtasks(schedule_command(self.hours.value()))
        self.say(("Scheduled: " if ok else "Schedule failed: ") + text)

    def unschedule(self) -> None:
        ok, text = run_schtasks(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        self.say(("Removed: " if ok else "Remove failed: ") + text)


def start(settings_path: Path) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = Window(settings_path)
    window.show()
    return app.exec()
