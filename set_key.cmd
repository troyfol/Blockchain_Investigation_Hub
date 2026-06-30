@echo off
rem Double-click to store your Etherscan V2 API key in the OS keyring (hidden prompt).
rem Opens its own console; the key is never echoed, logged, or written to disk.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo venv not found - run "make setup" first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\set_key.py %*
echo.
pause
