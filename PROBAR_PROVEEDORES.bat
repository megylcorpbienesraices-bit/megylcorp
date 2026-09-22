@echo off
cd /d "%~dp0"
echo ============================================================
echo ITM QUANT v1.57.0 - PRUEBA DE FLUJO REAL DE PROVEEDORES
echo ============================================================
echo.
set /p ITMQ_PROVIDER_SYMBOL=Simbolo a probar [DIA]: 
if "%ITMQ_PROVIDER_SYMBOL%"=="" set ITMQ_PROVIDER_SYMBOL=DIA
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Primero ejecuta INSTALAR_WEB.bat.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\test_provider_flow.py %ITMQ_PROVIDER_SYMBOL%
echo.
echo Esta prueba NO muestra API keys y NO hace llamadas a brokers.
echo Lee exclusivamente el flujo ya observado por ITM QUANT.
echo.
pause
