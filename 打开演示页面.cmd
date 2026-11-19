@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%~dp0.venv-libs"
set "PYTHONIOENCODING=utf-8"
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\demo_serve.py" %*
if errorlevel 1 (
  echo.
  echo [X] 启动失败：先确认 .venv 存在，或直接跑
  echo     python -m contentops.cli serve --port 8124
  pause
)
