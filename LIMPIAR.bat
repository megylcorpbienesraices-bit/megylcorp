@echo off
REM ============================================================================
REM  LIMPIAR - ITM QUANT
REM
REM  Borra UNICAMENTE lo que Python y las herramientas regeneran solas:
REM
REM    __pycache__\    bytecode compilado           se regenera al arrancar
REM    *.pyc *.pyo     idem, sueltos                se regenera al arrancar
REM    .pytest_cache\  cache de la suite            se regenera al probar
REM    .ruff_cache\    cache del linter             se regenera al analizar
REM    .mypy_cache\    cache de tipos               se regenera al analizar
REM
REM  NO TOCA NADA MAS. En concreto NO borra:
REM    .venv\          el entorno, que cuesta minutos reinstalar
REM    app\storage\    la memoria cuantitativa y el historico de sesiones
REM    .env            tus credenciales
REM    logs, datos de mercado, ni ningun fichero del programa
REM ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ===============================================================
echo   LIMPIAR RESIDUOS - solo caches regenerables
echo ===============================================================
echo.

set /a N=0
for /d /r %%d in (__pycache__) do (
  if exist "%%d" (rd /s /q "%%d" 2>nul & set /a N+=1)
)
for /d %%d in (.pytest_cache .ruff_cache .mypy_cache) do (
  if exist "%%d" (rd /s /q "%%d" 2>nul & set /a N+=1)
)
del /s /q *.pyc >nul 2>&1
del /s /q *.pyo >nul 2>&1

echo   Carpetas de cache borradas: !N!
echo   Bytecode suelto borrado.
echo.
echo   Intactos: .venv, app\storage, .env, logs y datos de mercado.
echo ===============================================================
echo.
pause
