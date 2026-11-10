from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook

from aoi_metadata import engine
from aoi_metadata.__main__ import main
from aoi_metadata.models import NOT_VERIFIED
from aoi_metadata.reader import code
from aoi_metadata.scan import buildup_name, panel_name
from aoi_metadata.settings import Settings, SettingsError, load, save

HEADER = ["DEFECT_ID", "DEFECT_TYPE", "X_COORDINATES", "Y_COORDINATES", "UNIT_INDEX_X",
          "UNIT_INDEX_Y", "MODALITY_1", "VERIFICATION", "ACCEPT"]  # fmt: skip


def workbook(path: Path, verifications: list[object], start_id: int = 1,
             header: list[str] | None = None, sheet: str = "Defects") -> Path:  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.append(["Lot", "x"])
    ws = wb.create_sheet(sheet)
    ws.append(header or HEADER)
    for i, verification in enumerate(verifications):
        ws.append([start_id + i, "SHORT", 1.0, 2.0, 3, 4, "img", verification, None])
    wb.save(path)
    return path


@pytest.fixture()
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "130 - measurement results"
    lot = root / "01_Customer shippable lots" / "Chiplet4Future" / "FHR0020" / "19621"
    back = lot / "AOI" / "BU-01" / "Post FET" / "Panel 3202" / "Back"
    files = {
        # split at 500: two files, ids carry on
        "split1": workbook(back / "scan_1.xlsx", ["N"] * 300 + ["CU41"] * 200, 1),
        "split2": workbook(back / "scan_2.xlsx", [" cu41", None, "", "TA"], 501),
        "front": workbook(
            lot / "AOI" / "BU02" / "Post FET" / "Panel 3202" / "front" / "a.xlsx",
            ["N", "N", "CU22"],
        ),  # fmt: skip
        "empty": workbook(
            lot / "AOI" / "BU-01" / "Post FET" / "Panel 3203" / "Front" / "e.xlsx", []
        ),  # fmt: skip
    }
    eng = root / "02_Engineering lots" / "ProjX" / "PART1" / "20001" / "AOI" / "BU-03" / "Post SR"
    files["rescan1"] = workbook(eng / "Panel 7" / "Back" / "r1.xlsx", ["N", "CU10"], 1)
    files["rescan2"] = workbook(eng / "Panel 7" / "Back" / "r2.xlsx", ["N", "CU10"], 1)
    files["nosheet"] = workbook(eng / "Panel 8" / "Front" / "x.xlsx", ["N"], sheet="Other")
    (eng / "Panel 8" / "Front" / "old.xls").write_bytes(b"old")
    (eng / "Panel 8" / "Front" / "broken.xlsx").write_bytes(b"not a zip")
    workbook(eng / "Panel 9" / "Side A" / "s.xlsx", ["N"])  # wrong side folder
    workbook(eng / "misplaced.xlsx", ["N"])  # file in the process folder
    workbook(lot / "BU-01" / "HRM" / "Panel 1" / "front" / "hrm.xlsx", ["N"])  # not AOI
    return {"root": root, **files}


@pytest.fixture()
def settings(tree: dict[str, Path], tmp_path: Path) -> Settings:
    return Settings(source_root=str(tree["root"]), output_dir=str(tmp_path / "out"),
                    cache_dir=str(tmp_path / "cache"), workers=2)  # fmt: skip


def test_names() -> None:
    assert buildup_name("BU-01") == buildup_name("bu01") == buildup_name("BU_1") == "BU-01"
    assert buildup_name("HRM") is None
    assert panel_name("Panel 3202") == "3202"
    assert panel_name("Post FET") is None
    assert code(None) == code("  ") == NOT_VERIFIED
    assert code(" cu41 ") == "CU41"
    assert code(1.0) == "1"


def test_run_builds_long_wide_and_issues(settings: Settings) -> None:
    report = engine.run(settings, engine.RunOptions())
    out = report.output
    data = pd.read_parquet(out / "AOI_Data.parquet")
    files = pd.read_parquet(out / "AOI_Files.parquet")
    issues = pd.read_parquet(out / "AOI_Issues.parquet")

    back = data[(data["Panel"] == "3202") & (data["Side"] == "Back")]
    assert back.groupby("Verification")["Count"].sum().to_dict() == {
        "N": 300, "CU41": 201, "TA": 1, NOT_VERIFIED: 2,
    }  # fmt: skip
    row = back.iloc[0]
    assert (row["Lot Type"], row["Project"], row["Part Number"], row["Lot Number"]) == (
        "Shippable", "Chiplet4Future", "FHR0020", "19621")  # fmt: skip
    assert (row["Buildup"], row["Process"], row["Lot Key"]) == ("BU-01", "Post FET",
                                                                 "Shippable|19621")  # fmt: skip
    assert set(data["Buildup"]) == {"BU-01", "BU-02", "BU-03"}
    assert "hrm.xlsx" not in set(files["Source File"])

    wide = pd.read_excel(
        out / "AOI_Wide.xlsx", sheet_name="Wide", dtype={"Panel": str, "Lot Number": str}
    )
    side = wide[(wide["Panel"] == "3202") & (wide["Side"] == "Back")].iloc[0]
    assert side["Files"] == 2 and side["Total Defects"] == 504
    assert side["N"] == 300 and side["CU41"] == 201 and side[NOT_VERIFIED] == 2
    assert list(wide.columns)[-1] == NOT_VERIFIED
    empty = wide[wide["Panel"] == "3203"].iloc[0]
    assert empty["Total Defects"] == 0 and empty["N"] == 0
    assert load_workbook(out / "AOI_Wide.xlsx").sheetnames == ["Wide", "Files", "Issues"]

    categories = set(issues["Category"])
    assert {"possible_rescan", "missing_sheet", "legacy_xls", "unreadable_workbook",
            "folder_layout", "misplaced_workbook"} <= categories  # fmt: skip
    rescan = issues[issues["Category"] == "possible_rescan"].iloc[0]
    assert "2 DEFECT_ID" in rescan["Message"]
    assert issues[issues["Category"] == "possible_rescan"].shape[0] == 1  # not split files
    assert report.run["Files Failed"] == 2


def test_second_run_reads_only_changed(settings: Settings, tree: dict[str, Path]) -> None:
    engine.run(settings, engine.RunOptions())
    second = engine.run(settings, engine.RunOptions())
    assert second.run["Files Read"] == 2  # the two broken files are tried again
    workbook(tree["front"], ["N"])
    os.utime(tree["front"], (1, 1))
    tree["rescan2"].unlink()
    third = engine.run(settings, engine.RunOptions())
    assert third.run["Files Read"] == 3 and third.run["Files Removed"] == 1
    data = pd.read_parquet(third.output / "AOI_Data.parquet")
    assert data[data["Buildup"] == "BU-02"]["Count"].sum() == 1
    runs = pd.read_parquet(third.output / "AOI_Runs.parquet")
    assert len(runs) == 3


def test_test_run_uses_test_folder(settings: Settings) -> None:
    report = engine.run(settings, engine.RunOptions(test_project=("Engineering", "ProjX")))
    assert report.output == settings.cache / "test" / "output"
    data = pd.read_parquet(report.output / "AOI_Data.parquet")
    assert set(data["Project"]) == {"ProjX"}
    assert not settings.output.exists()
    with pytest.raises(engine.RunError):
        engine.run(settings, engine.RunOptions(test_project=("Engineering", "Nope")))


def test_positional_and_missing_verification(settings: Settings, tree: dict[str, Path]) -> None:
    folder = tree["front"].parent
    workbook(folder / "odd.xlsx", ["N", "CU22"], 100,
             header=["a", "b", "c", "d", "e", "f", "g", "h", "i"])  # fmt: skip
    workbook(folder / "nover.xlsx", ["x"], 200, header=HEADER[:6] + ["MODALITY_1", "Z", "Q"])
    report = engine.run(settings, engine.RunOptions())
    data = pd.read_parquet(report.output / "AOI_Data.parquet")
    odd = data[data["Source File"] == "odd.xlsx"].set_index("Verification")["Count"]
    assert odd.to_dict() == {"CU22": 1, "N": 1}
    nover = data[data["Source File"] == "nover.xlsx"]
    assert nover["Verification"].tolist() == [NOT_VERIFIED]
    issues = pd.read_parquet(report.output / "AOI_Issues.parquet")
    assert {"positional_columns", "no_verification_column"} <= set(issues["Category"])


def test_lock_blocks_second_run(settings: Settings) -> None:
    with engine.RunLock(settings.cache), pytest.raises(engine.RunError):
        engine.run(settings, engine.RunOptions())


def test_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "aoi_settings.yaml"
    assert load(path) == Settings()
    settings = Settings(output_dir="X:/out", workers=8)
    save(settings, path)
    assert load(path) == settings
    path.write_text("workers: zero\n")
    with pytest.raises(SettingsError):
        load(path)
    path.write_text("nonsense: 1\n")
    with pytest.raises(SettingsError):
        load(path)


def test_cli_run(settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "s.yaml"
    save(settings, path)
    assert main(["--settings", str(path), "run"]) == 0
    assert "Files Found" in capsys.readouterr().out
    assert main(["--settings", str(path), "run", "--test", "bad"]) == 1
