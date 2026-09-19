@echo off
setlocal
cd /d "%~dp0..\frontend\solid-shell"
where node >nul 2>nul || (echo [ERROR] Node.js no esta instalado.& exit /b 1)
where npm >nul 2>nul || (echo [ERROR] npm no esta instalado.& exit /b 1)
if not exist package-lock.json (echo [ERROR] Falta package-lock.json; el shell Solid es REFERENCE/OPTIONAL y no se resuelve durante build.& exit /b 1)
call npm ci --ignore-scripts --no-audit --no-fund || exit /b 1
call npm run check || exit /b 1
call npm run build || exit /b 1
endlocal
