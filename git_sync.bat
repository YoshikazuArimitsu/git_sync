@echo off
rem git_sync - Windows launcher
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python git_sync.py %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
