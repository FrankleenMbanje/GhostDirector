@echo off
cd /d "%~dp0"
echo Starting local Fame Files production. Uploads remain disabled.
set PYTHONUTF8=1
"venv\Scripts\python.exe" -m local_studio --daily
exit /b %errorlevel%
