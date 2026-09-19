@echo off
setlocal
where wasm-pack >nul 2>nul || (echo [ERROR] wasm-pack no esta instalado; instalalo fuera del release gate.& exit /b 1)
cd /d "%~dp0..\rust\wasm_bridge"
if not exist Cargo.lock (echo [ERROR] Falta Cargo.lock; ejecuta prepare_release_assets.py fuera del gate.& exit /b 1)
wasm-pack build --locked --target web --release --out-dir "..\..\app\static\wasm"
