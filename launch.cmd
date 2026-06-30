@echo off
rem One-click launcher (Windows) — double-click to open the Investigation Hub desktop window.
rem Runs the project venv's Python on the launcher (which serves the app + opens a native window).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo venv not found - run "make setup" first ^(needs Python 3.12+^).
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\launch.py %*
if errorlevel 1 pause
