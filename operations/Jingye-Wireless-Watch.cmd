@echo off
setlocal
chcp 65001 >nul
title Jingye Wireless Watch
"C:\Users\YOUR_USER\OpenClawNative\python-venv\Scripts\python.exe" -B -X utf8 "%~dp0jingye_wireless_status.py" --watch
