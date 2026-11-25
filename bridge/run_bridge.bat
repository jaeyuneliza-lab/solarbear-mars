@echo off
cd /d "%~dp0"
rem open the page after the server has had a few seconds to start
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:8770"
python bridge.py
echo.
echo The bridge stopped. If you see an error above, send it to the team.
pause
