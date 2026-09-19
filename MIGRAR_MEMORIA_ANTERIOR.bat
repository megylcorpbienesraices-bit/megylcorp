@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Primero ejecuta INSTALAR_WEB.bat.
  pause
  exit /b 1
)
echo.
echo ============================================================
echo ITM QUANT - MIGRACION SEGURA DE MEMORIA CUANTITATIVA
echo ============================================================
echo.
echo Este asistente NO escanea carpetas vecinas.
echo Tu seleccionas exactamente la instalacion anterior.
echo.
set /p OLD=Ruta de la carpeta anterior (ej. ...v1.16.8): 
if "%OLD%"=="" goto :end
".venv\Scripts\python.exe" migrate_data.py "%OLD%"
:end
echo.
pause
