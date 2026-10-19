@echo off
rem Local-only daily production. Never invoke the retired cloud/uploader lane.
cd /d "%~dp0.."
set PYTHONUTF8=1
"venv\Scripts\python.exe" -m local_studio --daily
exit /b %errorlevel%
