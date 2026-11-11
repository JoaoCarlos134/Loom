@echo off
rem Loom in its own window. Double-click, or run from a terminal.
rem The first run installs the dependencies (uv sync), which takes a minute.
rem Falls back to the browser by itself if no webview runtime is usable.
cd /d "%~dp0"
rem OneDrive locks hard links mid-sync; copying avoids "Access denied".
set UV_LINK_MODE=copy
where uv >nul 2>nul || (
  echo uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv run python app.py %*
if errorlevel 1 pause
