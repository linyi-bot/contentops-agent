@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%~dp0.venv-libs"
set "PYTHONIOENCODING=utf-8"
python "%~dp0scripts\demo_run.py" %*
echo.
pause
