@echo off
REM StockSophist Charts - STOP the chart server completely.
REM Kills the chart app's Python process(es). NEVER touches MT5 (terminal64.exe).
cd /d "%~dp0"
echo [chart] stopping StockSophist chart server...

REM 1. Kill by PID file (most precise - only our server).
REM     PID lives in %TEMP% so it survives folder deletion.
if exist "%TEMP%\stocksophist_chart_server.pid" (
    for /f %%p in (%TEMP%\stocksophist_chart_server.pid) do (
        echo [chart] stopping PID %%p ...
        taskkill /PID %%p /F >nul 2>&1
    )
    del "%TEMP%\stocksophist_chart_server.pid" >nul 2>&1
)
REM     (legacy location, for servers started by older versions)
if exist "app\server.pid" (
    for /f %%p in (app\server.pid) do (
        echo [chart] stopping PID %%p ...
        taskkill /PID %%p /F >nul 2>&1
    )
    del "app\server.pid" >nul 2>&1
)

REM 2. Kill by window title (catches servers started via start_chart.bat).
taskkill /FI "WINDOWTITLE eq StockSophistChartServer*" /F >nul 2>&1

REM 3. Fallback: any python.exe whose command line points at the StockSophist chart server.
REM    (MT5 is terminal64.exe - never matched, never touched.)
for /f "tokens=2 delims=," %%p in (
    'wmic process where "name='python.exe' and commandline like '%%stocksophist%%server.py%%'" get processid /format:csv 2^>nul ^| findstr /r "[0-9]"'
) do (
    echo [chart] stopping stray python PID %%p ...
    taskkill /PID %%p /F >nul 2>&1
)
for /f "tokens=2 delims=," %%p in (
    'wmic process where "name='pythonw.exe' and commandline like '%%stocksophist%%server.py%%'" get processid /format:csv 2^>nul ^| findstr /r "[0-9]"'
) do (
    taskkill /PID %%p /F >nul 2>&1
)

REM 4. Kill ANYTHING listening on port 5000 (the chart port).
REM    This catches servers started any other way (double-clicked server.py,
REM    old folders, etc). MT5 never uses port 5000 - always safe.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r ":5000[^0-9]" ^| findstr "LISTENING"') do (
    echo [chart] stopping process on port 5000 ^(PID %%p^) ...
    taskkill /PID %%p /F >nul 2>&1
)

echo [chart] done. MT5 terminal was NOT touched.
echo [chart] you can now safely extract a new version or run start_chart.bat.
pause
