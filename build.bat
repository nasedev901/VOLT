@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" not found. Install Python 3.10+ from python.org first.
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip || exit /b 1
rem the package "hid" and the package "hidapi" both provide `import hid` - only hidapi is wanted
python -m pip uninstall -y hid >nul 2>nul
python -m pip install -r requirements.txt || exit /b 1

python -m PyInstaller --noconfirm --clean VOLT.spec || exit /b 1

echo.
if exist dist\VOLT\VOLT.exe echo Built: %cd%\dist\VOLT\VOLT.exe
if exist dist\VOLT.exe echo Built: %cd%\dist\VOLT.exe
endlocal
