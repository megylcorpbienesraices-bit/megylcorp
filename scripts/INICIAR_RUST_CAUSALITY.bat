@echo off
setlocal
set EXE=%~dp0..\rust\causality_engine\target\release\itm_causality_engine.exe
if not exist "%EXE%" (echo Primero ejecuta scripts\BUILD_RUST_CAUSALITY.bat& exit /b 1)
set ITM_RUST_ZMQ_BIND=tcp://127.0.0.1:5555
set ITM_RUST_INGEST_BIND=tcp://127.0.0.1:5554
"%EXE%"
