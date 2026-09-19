@echo off
setlocal
where cargo >nul 2>nul || (echo Rust/Cargo no esta instalado.& exit /b 1)
cd /d "%~dp0..\rust\causality_engine"
if not exist Cargo.lock (echo [ERROR] Falta Cargo.lock; ejecuta prepare_release_assets.py fuera del gate.& exit /b 1)
cargo build --locked --release
if errorlevel 1 exit /b 1
endlocal
