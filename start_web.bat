@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  start_web.bat - start Multi-Agent Analytics frontend (Vue3+ECharts)
REM  Site: http://localhost:5173
REM  Requires backend running first (start_api.bat, port 8000).
REM ============================================================
cd /d "%~dp0web"

if exist "node_modules" goto run

echo [install] Installing web dependencies
echo [install] This may take a minute, please wait...
call npm install
if errorlevel 1 goto install_failed

:run
echo [start] Frontend on http://localhost:5173
echo [tips]  Keep the backend running; answers stream to this page.
call npm run dev
goto end

:install_failed
echo [error] npm install failed.
echo [error] Make sure Node.js (>=18) is installed and npm is on PATH.
pause

:end
endlocal