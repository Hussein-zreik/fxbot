@echo off
REM GoldBot launcher: restarts the bot automatically if it exits or crashes.
cd /d "%~dp0"
title GoldBot
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
:loop
echo [%date% %time%] Starting GoldBot...
python run_bot.py --config config.json --secrets secrets.env
echo [%date% %time%] GoldBot exited with code %errorlevel%. Restarting in 30 seconds...
REM ping is the pause here because it also works without a console
ping -n 31 127.0.0.1 >nul
goto loop
