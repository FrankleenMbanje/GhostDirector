@echo off
REM ─────────────────────────────────────────────────────────────
REM The Fame Files — daily trending celebrity news short
REM Picks today's top trending story, produces the 9:16 short,
REM uploads it PUBLIC to Fame Files, writes trending_short.log.
REM
REM One-time Task Scheduler setup (run in cmd):
REM   schtasks /Create /TN "FameFiles Daily" /SC DAILY /ST 17:00 ^
REM     /TR "C:\path\to\GhostDirector\daily_famefiles.bat"
REM ─────────────────────────────────────────────────────────────
cd /d "%~dp0"

venv\Scripts\python.exe main.py --trending --privacy unlisted --channel famefiles >> trending_short.log 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo [%date% %time%] FAILED exit=%ERRORLEVEL% >> trending_short.log
) else (
  echo [%date% %time%] OK >> trending_short.log
)
