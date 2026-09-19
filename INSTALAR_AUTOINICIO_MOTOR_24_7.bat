@echo off
setlocal
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo Primero ejecuta INSTALAR_WEB.bat.
  pause
  exit /b 1
)
set TASKNAME=ITM_QUANT_ALWAYS_ON_ENGINE
set RUNNER=%~dp0INICIAR_MOTOR_24_7.bat
schtasks /Create /F /SC ONLOGON /TN "%TASKNAME%" /TR "\"%RUNNER%\"" >nul
if errorlevel 1 (
  echo No se pudo crear la tarea. Ejecuta este archivo como Administrador.
  pause
  exit /b 1
)
echo.
echo LISTO: %TASKNAME% se iniciara automaticamente al entrar a Windows.
echo IMPORTANTE: esto mantiene el motor activo mientras ESTA PC este encendida.
echo Para que siga trabajando con la PC apagada, despliega la carpeta en un VPS 24/7 usando docker-compose.always-on.yml.
echo.
pause
