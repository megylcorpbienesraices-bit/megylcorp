@echo off
setlocal
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo FALTA LA INSTALACION AISLADA. Ejecuta INSTALAR_WEB.bat primero.
  pause
  exit /b 1
)
for /f "delims=" %%A in ('powershell -NoProfile -Command "try{(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/healthz -TimeoutSec 1).StatusCode}catch{0}"') do set STATUS=%%A
if "%STATUS%"=="200" (
  echo ITM QUANT ALWAYS-ON ya esta trabajando en segundo plano.
  exit /b 0
)

rem Baja latencia: si existe el motor Rust, arranca junto al host persistente.
set RUSTEXE=%~dp0rust\causality_engine\target\release\itm_causality_engine.exe
if exist "%RUSTEXE%" (
  set ITM_RUST_CAUSALITY=1
  set ITM_RUST_PYTHON_FORWARD=1
  set ITM_RUST_INGEST_BIND=tcp://127.0.0.1:5554
  set ITM_RUST_INGEST_ENDPOINT=tcp://127.0.0.1:5554
  set ITM_RUST_ZMQ_BIND=tcp://127.0.0.1:5555
  start "ITM QUANT RUST CAUSALITY" /min "%RUSTEXE%"
)
set ITM_ALWAYS_ON=1
start "ITM QUANT ALWAYS-ON ENGINE" /min ".venv\Scripts\python.exe" run_always_on.py
exit /b 0
