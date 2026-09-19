@echo off
setlocal
cd /d %~dp0
call INICIAR_MOTOR_24_7.bat
if errorlevel 1 exit /b 1
start "" http://127.0.0.1:8000
endlocal
