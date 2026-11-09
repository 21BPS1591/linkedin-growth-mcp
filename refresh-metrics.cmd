@echo off
cd /d "%~dp0"
echo ===================================================
echo   Refreshing live metrics for all LinkedIn posts
echo ===================================================
echo.
echo Connecting to LinkedIn to read latest analytics...
echo Please wait, this takes about 20-30 seconds.
echo.

".venv\Scripts\python.exe" -m linkedin_growth_mcp --refresh-metrics

echo.
echo All metrics are up to date!
echo Check your dashboard at: http://127.0.0.1:8790
echo ===================================================
echo.
pause
