@echo off
rem Run Bookfinch directly with Python (no build needed).
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 app.py & goto :eof)
where pythonw >nul 2>nul && (start "" pythonw app.py & goto :eof)
echo You need Python 3.9 or newer: https://www.python.org/downloads/
pause
