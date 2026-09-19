@echo off
setlocal
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" goto :noenv
".venv\Scripts\python.exe" setup_local.py
pause
exit /b %errorlevel%
:noenv
echo ERROR: Primero ejecuta INSTALAR_WEB.bat.
pause
exit /b 1
