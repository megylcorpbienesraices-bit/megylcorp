@echo off
setlocal
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" goto :noenv
".venv\Scripts\python.exe" setup_local_gui.py
if errorlevel 1 (
  echo.
  echo No se pudo abrir la ventana de configuracion.
  pause
  exit /b 1
)
exit /b 0
:noenv
echo ERROR: Primero ejecuta INSTALAR_WEB.bat.
pause
exit /b 1
