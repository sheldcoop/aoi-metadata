@echo off
rem AOI metadata: defects per Verification code for every AOI workbook.
rem   Double-click        : opens the window
rem   AOI.bat scheduled   : one run without a window (used by the scheduled task)
rem   AOI.bat run [...]   : one run with options, e.g. AOI.bat run --test "Shippable/Chiplet4Future"
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: creating the Python environment...
    py -m venv .venv || goto :error
)
rem Install again whenever pyproject.toml changed (a new version was pulled).
fc /b pyproject.toml ".venv\pyproject.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing the packages, this takes a few minutes the first time...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e . || goto :error
    copy /y pyproject.toml ".venv\pyproject.installed" >nul
)
if /i "%~1"=="scheduled" (
    ".venv\Scripts\python.exe" -m aoi_metadata run
) else if /i "%~1"=="run" (
    ".venv\Scripts\python.exe" -m aoi_metadata %*
) else (
    start "" ".venv\Scripts\pythonw.exe" -m aoi_metadata
    exit /b 0
)
if errorlevel 1 goto :error
if /i "%~1"=="run" pause
exit /b 0
:error
echo.
echo The AOI export did not finish. See the message above.
if /i not "%~1"=="scheduled" pause
exit /b 1
