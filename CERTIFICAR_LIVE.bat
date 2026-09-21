@echo off
REM ============================================================================
REM  CERTIFICACION LIVE - ITM QUANT
REM
REM  Prueba DIA, SPY y QQQ en UNA sola ejecucion y deja, herramienta por
REM  herramienta, el estado exacto y si el dato llega a su modulo.
REM
REM  ANTES DE EJECUTAR:
REM    1. La terminal tiene que estar LEVANTADA (INICIAR_WEB.bat)
REM    2. QUANTDATA_API_KEY tiene que estar puesta
REM
REM  El informe NO contiene claves ni credenciales.
REM ============================================================================
setlocal
cd /d "%~dp0"

echo.
echo ===============================================================
echo   CERTIFICACION LIVE - DIA / SPY / QQQ
echo ===============================================================
echo.

REM --- 1. Python -------------------------------------------------------------
where py >/dev/null 2>&1
if %ERRORLEVEL%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version >/dev/null 2>&1
if not %ERRORLEVEL%==0 (
  echo [ERROR] No se encuentra Python. Instalalo o abre el entorno del proyecto.
  pause
  exit /b 2
)

REM --- 2. httpx --------------------------------------------------------------
%PY% -c "import httpx" >/dev/null 2>&1
if not %ERRORLEVEL%==0 (
  echo Instalando httpx...
  %PY% -m pip install --quiet httpx
)

REM --- 3. La API key tiene que estar puesta ----------------------------------
if "%QUANTDATA_API_KEY%"=="" (
  echo [ERROR] QUANTDATA_API_KEY no esta definida en esta ventana.
  echo.
  echo   Ponla asi, en ESTA misma ventana, antes de volver a ejecutar:
  echo      set QUANTDATA_API_KEY=tu_clave_aqui
  echo.
  echo   La clave NO aparece en el informe: se redacta antes de escribirlo.
  pause
  exit /b 2
)

REM --- 4. La terminal tiene que responder ------------------------------------
if "%ITMQ_BASE_URL%"=="" set "ITMQ_BASE_URL=http://127.0.0.1:8000"
echo Comprobando que la terminal responde en %ITMQ_BASE_URL% ...
%PY% -c "import httpx,os,sys; httpx.get(os.environ[\x27ITMQ_BASE_URL\x27]+\x27/api/terminal/bundle\x27,timeout=30).raise_for_status()" >/dev/null 2>&1
if not %ERRORLEVEL%==0 (
  echo [ERROR] La terminal no responde en %ITMQ_BASE_URL%
  echo         Arrancala primero con INICIAR_WEB.bat y espera a que cargue.
  pause
  exit /b 2
)
echo   OK
echo.

REM --- 5. Certificacion ------------------------------------------------------
%PY% scripts\\certificar_live.py --base-url %ITMQ_BASE_URL% --symbols DIA,SPY,QQQ --out CERTIFICACION_LIVE.json
set "RC=%ERRORLEVEL%"

echo.
echo ===============================================================
if exist CERTIFICACION_LIVE.json (
  echo   LISTO. Envia este fichero:
  echo      %CD%\\CERTIFICACION_LIVE.json
) else (
  echo   No se genero el informe. Revisa los mensajes de arriba.
)
echo ===============================================================
echo.
pause
exit /b %RC%
