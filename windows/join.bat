@echo off
REM agentchattr - joins Claude Code or Codex on this PC to a chat server on the LAN.
REM Usage: join.bat <profile>     (profiles live in client.local.toml)
REM No tmux, no local server: the token decides the identity and which CLI starts.
cd /d "%~dp0.."

if "%~1"=="" (
    echo.
    echo   Usage: join.bat ^<profile^>
    echo   Profiles are defined in client.local.toml ^(copy client.local.toml.example^).
    echo.
    pause
    exit /b 1
)

REM Auto-create venv and install deps on first run
if not exist ".venv" (
    python -m venv .venv
    .venv\Scripts\pip install -q -r requirements.txt >nul 2>nul
)
call .venv\Scripts\activate.bat

python client.py %*
if %errorlevel% neq 0 (
    echo.
    echo   Client exited with an error. Check the output above.
    pause
)
