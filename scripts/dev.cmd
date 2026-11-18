@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0.."
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%\src;%ROOT%\.venv-libs"
set "PYTHONIOENCODING=utf-8"

if /i "%1"=="test" (
    rem shift does not change %* -- take %2..%9 explicitly
    python -m pytest -q -p no:cacheprovider tests %2 %3 %4 %5 %6 %7 %8 %9
    exit /b %ERRORLEVEL%
)

python -m contentops.cli %*
exit /b %ERRORLEVEL%
