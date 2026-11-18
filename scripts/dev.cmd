@echo off
:: ContentOps Agent 开发入口
:: 作用：把 src 和本地依赖目录加进 PYTHONPATH，再跑命令，避免污染系统 Python。
::
::   scripts\dev.cmd run --topic "用 AI 做周报自动化"
::   scripts\dev.cmd kb --query "事实核查"
::   scripts\dev.cmd test

setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%ROOT%\.venv-libs"
set "PYTHONIOENCODING=utf-8"

if /i "%1"=="test" (
    shift
    :: -p no:cacheprovider：这台机器上 pytest 的缓存目录会被沙箱拦住，不是必须功能
    python -m pytest -q -p no:cacheprovider %*
    exit /b %ERRORLEVEL%
)

python -m contentops.cli %*
exit /b %ERRORLEVEL%
