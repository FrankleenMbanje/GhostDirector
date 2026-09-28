@echo off
rem GhostDirector daily production (Task Scheduler entry point).
rem Output is appended to output\_daily.log so an unattended 08:00 run
rem always leaves a diagnosable trail (FIX-068: scheduler hardening).
cd /d "C:\Users\frank\.gemini\antigravity\scratch\ghostdirector"
"venv\Scripts\python.exe" main.py --daily >> "output\_daily.log" 2>&1
