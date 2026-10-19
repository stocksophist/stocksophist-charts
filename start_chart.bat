@echo off
REM StockSophist Charts - local chart server
REM Run this on the host machine where MT5 terminal is installed and logged in.
REM FOLDER LAYOUT
REM   C:\stocksophist\app\ <- this folder (chart code, updated via zips)
REM   C:\stocksophist\data\ <- HISTORY DATABASE folder (you manage, app only reads)
REM                      put stocksophist_xauusd_m1_*.db here - never inside app\
REM Or set STOCKSOPHIST_DATA_DIR to point at your database folder.
cd /d "%~dp0"

REM kill any previous chart server first, so an old version can
REM never keep running underneath the new files. (MT5 is never touched.)
REM PID file is in %TEMP% (survives folder deletion/replace).
if exist "%TEMP%\stocksophist_chart_server.pid" (
    for /f %%p in (%TEMP%\stocksophist_chart_server.pid) do taskkill /PID %%p /F >nul 2>&1
    del "%TEMP%\stocksophist_chart_server.pid" >nul 2>&1
)
if exist "app\server.pid" (
    for /f %%p in (app\server.pid) do taskkill /PID %%p /F >nul 2>&1
    del "app\server.pid" >nul 2>&1
)
taskkill /FI "WINDOWTITLE eq StockSophistChartServer*" /F >nul 2>&1
REM also kill anything on port 5000 (catches servers started any
REM other way). MT5 never uses port 5000 - always safe.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r ":5000[^0-9]" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
REM wait up to 10s for port 5000 to free up (zombie processes can
REM take a moment to die). If still busy, warn instead of starting a broken
REM second server.
set _tries=0
:waitport
netstat -ano | findstr /r ":5000[^0-9]" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 goto portfree
set /a _tries+=1
if %_tries% GEQ 10 goto portbusy
timeout /t 1 /nobreak >nul
goto waitport
:portbusy
echo [chart] ERROR: port 5000 is still in use after 10s.
echo [chart] A zombie server is stuck. Open Task Manager, kill all python.exe,
echo [chart] then run start_chart.bat again. (MT5 terminal64.exe is NOT the problem - leave it.)
pause
exit /b 1
:portfree
echo [chart] port 5000 is free - starting fresh server.

python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [chart] pip install failed - check your internet connection
    pause
    exit /b 1
)
echo [chart] starting on http://127.0.0.1:5000 ...
echo [chart] the chart will open in your browser automatically.
echo [chart] on startup the server connects MT5 and syncs 1m history in the
echo [chart] background - the browser shows live sync progress, then the chart.
echo [chart] to STOP the server, run stop_chart.bat (MT5 stays running).
start "StockSophistChartServer" python app\server.py
timeout /t 6 /nobreak >nul
REM removed "start http://127.0.0.1:5000" - server.py already opens
REM the browser itself. Having both opened 2 tabs.
echo [chart] server is running in the "StockSophistChartServer" window.
pause
