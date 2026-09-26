@echo off
REM GoldBot launcher: restarts the bot automatically if it exits or crashes.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
:loop
echo [%date% %time%] Starting GoldBot...
python run_bot.py --config config.json
echo [%date% %time%] GoldBot exited with code %errorlevel%. Restarting in 30 seconds (Ctrl+C to abort)...
timeout /t 30 /nobreak >nul
goto loop
