@echo off
cd /d "%~dp0"
echo The Fame Files Local Studio: http://127.0.0.1:9042
echo Keep this window open while using the dashboard. Daily production runs separately.
"venv\Scripts\python.exe" -m local_studio --dashboard
pause
