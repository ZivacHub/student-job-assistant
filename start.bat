@echo off
setlocal
title Studentski poslovi - zatvorite ovaj prozor da zaustavite aplikaciju
cd /d "%~dp0"
if not defined JOBS_PORT set JOBS_PORT=8765

if not exist ".venv\Scripts\python.exe" (
  echo Prvo pokretanje: pripremam aplikaciju. Ovo traje nekoliko minuta...
  py -3.11 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 (
    echo Python 3.11 nije pronaden. Javite Claudeu.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -e .
  if errorlevel 1 (
    echo Instalacija nije uspjela. Javite Claudeu.
    pause
    exit /b 1
  )
)

echo.
echo Aplikacija radi na http://127.0.0.1:%JOBS_PORT%
echo Ostavite ovaj prozor otvoren. Kad ga zatvorite, aplikacija se zaustavlja.
echo.
if not defined NO_BROWSER start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:%JOBS_PORT%"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %JOBS_PORT%
echo.
echo Aplikacija je zaustavljena. Ako to nije bilo namjerno, procitajte poruku iznad.
pause
