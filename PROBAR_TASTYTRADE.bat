@echo off
setlocal
cd /d "%~dp0"
cls
echo ================================================================
echo ITM QUANT v1.45.0 - PRUEBA REAL TASTYTRADE OAUTH + DXLINK
echo ================================================================
echo.
set /p SYMBOL=Simbolo a probar [DIA]: 
if "%SYMBOL%"=="" set SYMBOL=DIA
set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
  echo ERROR: Primero ejecuta INSTALAR_WEB.bat.
  pause
  exit /b 1
)
"%PY%" scripts\test_tastytrade_live.py "%SYMBOL%"
echo.
pause
