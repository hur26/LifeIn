@echo off
setlocal

rem LifeIn resident launcher. Rationale for each step: see run-lifein.md
rem
rem ASCII only on purpose: cmd.exe loses sync on multi-byte comments even
rem under chcp 65001, and the symptom is a rem line executing as a command.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
set "LOGDIR=%~dp0..\logs"
set "LOG=%LOGDIR%\lifein.log"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

rem Single-instance guard, BEFORE touching the log file.
"%PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(1 if s.connect_ex(('127.0.0.1',8000)) else 0)" 2>nul
if not errorlevel 1 (
    echo LifeIn already running on port 8000, exiting.
    goto :eof
)

echo [%date% %time%] starting LifeIn>>"%LOG%"

rem Wait up to 5 minutes for PostgreSQL to accept connections.
set /a WAITED=0
:waitdb
"%PY%" -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if not s.connect_ex(('127.0.0.1',55432)) else 1)" 2>nul
if not errorlevel 1 goto dbready
set /a WAITED+=5
if %WAITED% GEQ 300 (
    echo [%date% %time%] database not ready after 5 min, starting anyway>>"%LOG%"
    goto dbready
)
timeout /t 5 /nobreak >nul
goto waitdb

:dbready
:loop
echo [%date% %time%] --- process start --->>"%LOG%"
"%PY%" -m lifein >>"%LOG%" 2>&1
echo [%date% %time%] --- process exited (code %errorlevel%), restarting in 30s --->>"%LOG%"
timeout /t 30 /nobreak >nul
goto loop
