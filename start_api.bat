@echo off
chcp 65001 >nul
setlocal
REM ============================================================
REM  start_api.bat - start Multi-Agent Analytics backend
REM  Swagger / OpenAPI:  http://localhost:8000/docs
REM  Frontend (separate): http://localhost:5173 (start_web.bat)
REM ============================================================
cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe

if not exist ".env" (
  if exist ".env.example" (
    echo [info] .env not found. Copying .env.example to .env
    echo [info] Fill LLM_API_KEY in .env to enable real LLM mode.
    copy /y ".env.example" ".env" >nul
  )
)

set SQLPA_DB_PATH=%~dp0data\olist\olist.db

echo [start] Backend on http://localhost:8000  (Swagger: /docs)
echo [data]  Using demo DB: %SQLPA_DB_PATH%
echo [tips]  Close this window or press Ctrl+C to stop.
"%PY%" -m uvicorn api:app --host 0.0.0.0 --port 8000
endlocal