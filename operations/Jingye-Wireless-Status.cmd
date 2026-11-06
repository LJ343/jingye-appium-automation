@echo off
setlocal
chcp 65001 >nul
title Jingye Wireless Status
"C:\Users\YOUR_USER\OpenClawNative\python-venv\Scripts\python.exe" -B -X utf8 "%~dp0jingye_wireless_status.py"
set "JINGYE_STATUS_EXIT=%ERRORLEVEL%"
pause
exit /b %JINGYE_STATUS_EXIT%
