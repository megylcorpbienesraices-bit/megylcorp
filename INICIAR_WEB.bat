@echo off
setlocal EnableDelayedExpansion
cd /d %~dp0
call INICIAR_MOTOR_24_7.bat
if errorlevel 1 exit /b 1
for /l %%I in (1,1,40) do (
  for /f "delims=" %%A in ('powershell -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/health -TimeoutSec 1).StatusCode}catch{0}"') do set STATUS=%%A
  if "!STATUS!"=="200" goto :open
  timeout /t 1 /nobreak >nul
)
:open
start "" http://127.0.0.1:8000
endlocal
