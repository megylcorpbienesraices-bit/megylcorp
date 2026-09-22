@echo off
setlocal EnableExtensions
cd /d %~dp0

rem CERTIFICACION LIVE DEL TRANSPORTE · mide contra la API real de Quant Data.
rem El interprete se LEE de .python-version; aqui no hay ninguna version escrita.
set /p ITMQ_PYTHON=<.python-version
echo ==============================================================
echo ITM QUANT - CERTIFICACION LIVE DEL TRANSPORTE
echo ==============================================================
echo Mide: plazos por endpoint, concurrencia, recuperacion tras timeout,
echo       aislamiento entre endpoints y entre activos, y donde se va el tiempo.
echo Duracion aproximada: 6-7 minutos con los valores por defecto.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: no hay entorno privado. Ejecuta primero INSTALAR_WEB.bat.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" scripts\certificar_transporte_live.py %*
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" (
  echo VEREDICTO: PASS - revisa CERTIFICACION_LIVE_TRANSPORTE.md
) else if "%RC%"=="2" (
  echo No se pudo medir: falta QUANTDATA_API_KEY en el .env.
) else (
  echo VEREDICTO: FAIL - el detalle de cada afirmacion esta en el .md y el .json
)
echo.
pause
exit /b %RC%
