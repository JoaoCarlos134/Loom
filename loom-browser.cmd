@echo off
rem Loom in your default browser. Close this console to stop the server.
rem The first run installs the dependencies (uv sync), which takes a minute.
cd /d "%~dp0"
rem OneDrive locks hard links mid-sync; copying avoids "Access denied".
set UV_LINK_MODE=copy
where uv >nul 2>nul || (
  echo uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv run python app.py --browser %*
if errorlevel 1 pause
