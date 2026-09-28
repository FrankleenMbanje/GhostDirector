@echo off
REM ─────────────────────────────────────────────────────────────
REM Rise and Ruin — reminder helper. Long-form videos are operator-run
REM (topic pick + review), so this only prints the standard command.
REM Lane unchanged: rise-and-fall documentaries, --shorts for the seed short.
REM ─────────────────────────────────────────────────────────────
cd /d "%~dp0"
echo Rise and Ruin: produce the next long video with:
echo   venv\Scripts\python.exe main.py --next-topic --channel riseandruin --shorts --upload --privacy public
echo or a chosen topic:
echo   venv\Scripts\python.exe main.py "TOPIC" --channel riseandruin --shorts --upload --privacy public
