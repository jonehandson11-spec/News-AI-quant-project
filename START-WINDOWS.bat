@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -m web.server --open-browser
) else (
    py -3 -m web.server --open-browser
)
if errorlevel 1 pause
