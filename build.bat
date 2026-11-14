@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem  build.bat        -> single portable .exe          dist\Bookfinch.exe              (+ zip)
rem  build.bat dir    -> folder (starts faster)        dist\Bookfinch\Bookfinch.exe    (+ zip)
set "MODE=onefile"
if /i "%~1"=="dir" set "MODE=dir"
if "%MODE%"=="onefile" (set "SPEC=bookfinch-onefile.spec") else (set "SPEC=bookfinch.spec")

echo === Building Bookfinch [%MODE%] ===
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

if not exist ".venv-build\Scripts\python.exe" (
    echo Creating build environment...
    %PY% -m venv .venv-build || goto fail
)
call ".venv-build\Scripts\activate.bat" || goto fail
python -m pip install --quiet --upgrade pip || goto fail
python -m pip install --quiet -r requirements-build.txt || goto fail

echo.
echo === Running tests ===
python -m unittest discover -s tests || goto testfail

echo.
echo === Packaging ===
python -m PyInstaller --noconfirm --clean %SPEC% || goto fail

if "%MODE%"=="onefile" (
    copy /y portable.txt dist\ >nul
    powershell -NoProfile -Command "Compress-Archive -Path 'dist\Bookfinch.exe','dist\portable.txt' -DestinationPath 'dist\Bookfinch-portable.zip' -Force"
    set "OUT=dist\Bookfinch.exe"
) else (
    copy /y portable.txt dist\Bookfinch\ >nul
    powershell -NoProfile -Command "Compress-Archive -Path 'dist\Bookfinch\*' -DestinationPath 'dist\Bookfinch-portable.zip' -Force"
    set "OUT=dist\Bookfinch\Bookfinch.exe"
    echo.
    echo WARNING: in this mode the .exe does NOT work on its own. Copy the WHOLE folder
    echo dist\Bookfinch ^(with its _internal subfolder^) or use the zip. If you move only
    echo the .exe you will get "Failed to load Python DLL".
)
echo.
echo Done:  %OUT%
echo Portable zip:  dist\Bookfinch-portable.zip
echo (To start from scratch, delete the build, dist and .venv-build folders)
pause
exit /b 0

:nopython
echo You need Python 3.9 or newer ONLY to build: https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during installation)
pause
exit /b 1

:testfail
echo.
echo The tests failed: not building. See the message above.
pause
exit /b 1

:fail
echo.
echo An error occurred during the build.
pause
exit /b 1
