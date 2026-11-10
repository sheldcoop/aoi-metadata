# AOI metadata export

Counts the AOI defects of every lot **per Verification code** (`N`, `TA`, `CU22`,
`CU41`, …) and writes them to a parquet table for the HRM + PRF dashboard app plus
an Excel file for your own analysis. Only new or changed workbooks are read on each
run, and anything that could not be read is listed in Issues.

```text
L:\ale\ics_htb3_rnd\130 - measurement results
   01_Customer shippable lots\<Project>\<Part>\<Lot>\AOI\<BU-01>\<Process>\Panel <id>\<Front|Back>\*.xlsx
   02_Engineering lots\...
   e.g. ...\Chiplet4Future\FHR0020\19621\AOI\BU-01\Post FET\Panel 3202\Back\
        │  AOI.bat  (the window, or automatically every few hours)
        ▼
Output folder (default %OneDriveCommercial%\AOI_Metadata)
   AOI_Wide.xlsx       one row per panel side, one column per Verification code
   AOI_Data.parquet    one row per workbook and Verification code (for the app)
   AOI_Files.parquet · AOI_Issues.parquet · AOI_Runs.parquet
```

## Start

1. Double-click **`AOI.bat`**. The first time it installs what it needs, which takes
   a few minutes. After a `git pull` it updates itself the same way.
2. In the window, set the two folders and press **Save folders**.
3. Under **What to run**, choose what a run covers:
   - **Lot types:** tick *01_Customer shippable lots* and/or *02_Engineering lots*.
   - **Projects:** click **Load projects** and tick the ones you want. Use the filter box
     to find one quickly.
   - **Lots:** type lot numbers, e.g. `19621, 19622`.

   Nothing ticked or typed means **everything**. A run for some projects or lots
   updates **only those**; every other lot keeps its rows in the output.
4. **Test run** runs the same choice but writes to a test folder on your PC, so it
   never touches the real output. **Open output folder** shows where that folder is.
5. **Run AOI** writes to the output folder. The first run of everything takes long;
   later runs only read new or changed workbooks. **Full reload** reads every workbook
   in the choice again.
6. **Schedule**: *Create / update* runs everything every few hours without a window.

From a command line:

```bat
AOI.bat                                          the window
AOI.bat scheduled                                one run (what the schedule does)
AOI.bat run --full-reload                        read every workbook again
AOI.bat run --lot-type Shippable                 customer shippable lots only
AOI.bat run --lot-type Engineering               engineering lots only
AOI.bat run --project "Shippable/Chiplet4Future" one project (repeat for more)
AOI.bat run --lot 19621,19622                    these lots only
AOI.bat run --project "Shippable/Chiplet4Future" --test   to the test folder
```

## How a workbook is counted

- Only the **`Defects`** sheet is read, and from it only **`VERIFICATION`** and
  **`DEFECT_ID`**. The image columns are ignored.
- Columns are found by their header name, and case and spaces don't matter. If the
  headers aren't recognised, the fixed AOI column order is used, just as the Faster AOI
  app does (`DEFECT_ID, DEFECT_TYPE, X, Y, UNIT_INDEX_X, UNIT_INDEX_Y, …,
  VERIFICATION, ACCEPT`), and an Issue is logged.
- Codes are trimmed and written in capitals, so ` cu41` counts as `CU41`. An empty
  cell counts as **`Not verified`**, so a side's total always equals its number of
  defect rows.
- The AOI writes at most **500 defects per workbook** and starts a new file after
  that. All workbooks in one side folder are **added up** into one row of
  `AOI_Wide.xlsx`, and the `Files` column says how many there were.
- **Possible re-scans**: if the same `DEFECT_ID` turns up in two workbooks of one
  side folder, the files may be a re-scan rather than a split, and that side would be
  counted twice. It is still added up, and listed in Issues as `possible_rescan`.

## The tables

| Table | One row per | Columns |
|---|---|---|
| `AOI_Wide.xlsx` → *Wide* | panel side | Lot Type, Project, Part Number, Lot Number, **Lot Name**, Buildup, Process, Panel, Side, Files, Total Defects, Last Measured, Folder, then one column per code (`Not verified` last) |
| `AOI_Data` | workbook × code | Lot Type … Side, Verification, Count, Source File, Path, Measured At, Loaded At, Lot Key |
| `AOI_Files` (also an Excel sheet) | workbook | Lot Type … Side, Source File, Defects, Read OK, Path, Measured At, Loaded At, Lot Key |
| `AOI_Issues` (also an Excel sheet) | problem | Severity, Category, Lot Type, Project, Message, Source File, Path, Found At |
| `AOI_Runs` | run | Run At, Mode, Version, Result, files found / read / unchanged / removed / failed, Side Folders, Defects, Issues, Seconds |

**Lot Name** is empty, ready for you to fill in on the *Wide* sheet. The next run
reads the names from the existing `AOI_Wide.xlsx` (by Lot Type + Lot Number) and
writes them again, so they are kept. Save and close the file before a run: if it is
open, a copy with the time in its name is written, and names typed into that copy are
not read back.

The column names, `Buildup` (`BU-01`), `Side` (`Front` / `Back`) and `Lot Key`
(`Shippable|19621`) match the HRM + PRF dashboard, so the tables can be joined. If
`AOI_Wide.xlsx` is open in Excel during a run, a copy with the time in its name is
written instead.

### Issues it reports

`unreadable_workbook`, `missing_sheet`, `empty_sheet`, `no_verification_column`,
`positional_columns`, `possible_rescan`, `legacy_xls` (old `.xls` files are not read),
`misplaced_workbook` (Excel files outside a side folder), `folder_layout` (a folder
that is not a buildup, panel or side), `no_workbook`, `empty_folder`,
`unreadable_folder`, `missing_folder`.

## Settings: `aoi_settings.yaml`

Written next to `AOI.bat` when you save in the window. It isn't in git, and a
missing file means the defaults:

```yaml
source_root: L:\ale\ics_htb3_rnd\130 - measurement results
lot_types: {01_Customer shippable lots: Shippable, 02_Engineering lots: Engineering}
skip_folders: [Others]
machine_folder: AOI
defects_sheet: Defects
output_dir: '%OneDriveCommercial%\AOI_Metadata'
cache_dir: '%LOCALAPPDATA%\AOI_Metadata'   # cache, logs, test output
workers: 4
schedule_hours: 4
```

## Development

```bash
pip install -e .[dev]
pytest
ruff check src tests
```

## Later

- An **AOI** tab in the HRM + PRF dashboard app that reads `AOI_Data.parquet`. The
  code in `scan.py` and `reader.py` follows the dashboard's `scan()` / `read()`
  sources, so it can move into `hrm_converter/dashboard/sources/aoi.py`.
- Final Inspection grades (`A` / `H` / `L` / `M`).
