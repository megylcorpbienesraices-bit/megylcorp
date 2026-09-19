@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d %~dp0

set /p ITMQ_VERSION=<VERSION.txt
rem Historical Docker/Linux certification target remains 3.12.14; Windows hotfix uses official CPython 3.12.10 x64.
set ITMQ_PYTHON=3.12.10
echo ==============================================================
echo ITM QUANT DOW SPECIALIZED v%ITMQ_VERSION% - WINDOWS %ITMQ_PYTHON%
echo ==============================================================
echo.
echo Instalacion aislada en .venv. No modifica otros programas.
echo Esta release de Windows requiere CPython %ITMQ_PYTHON% x64 exactamente.
echo Pip se normaliza primero desde requirements.bootstrap.lock.txt.
echo No se compilan pandas/numpy/scipy desde codigo fuente.
echo.

where py >nul 2>nul
if errorlevel 1 goto :no_launcher

py -3.12 -c "import sys,platform; assert sys.version_info[:2]==(3,12); assert sys.version_info[:3]==(3,12,10); assert platform.architecture()[0]=='64bit'; print(sys.version)" >nul 2>nul
if errorlevel 1 goto :no_py312

if exist ".venv\Scripts\python.exe" (
  for /f "delims=" %%V in ('".venv\Scripts\python.exe" -c "import sys; print('.'.join(map(str,sys.version_info[:3])))" 2^>nul') do set VENV_VER=%%V
  if not "!VENV_VER!"=="%ITMQ_PYTHON%" (
    echo [1/6] El .venv existente usa Python !VENV_VER!, no %ITMQ_PYTHON%.
    echo       Se eliminara SOLO .venv y se recreara correctamente.
    rmdir /s /q ".venv"
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/6] Creando entorno privado con Python %ITMQ_PYTHON% x64...
  py -3.12 -m venv .venv
  if errorlevel 1 goto :install_error
) else (
  echo [1/6] Entorno privado Python %ITMQ_PYTHON% verificado.
)

echo [2/6] Verificando interprete privado exacto...
".venv\Scripts\python.exe" -c "import sys,platform; assert sys.version_info[:2]==(3,12); assert sys.version_info[:3]==(3,12,10); assert platform.architecture()[0]=='64bit'; print('Python OK:',sys.version.split()[0],platform.machine())"
if errorlevel 1 goto :install_error

echo [3/6] Normalizando pip desde bootstrap lock hash-verificado...
".venv\Scripts\python.exe" -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.bootstrap.lock.txt
if errorlevel 1 goto :install_error

echo [4/6] Verificando pip exacto del bootstrap...
".venv\Scripts\python.exe" -c "import importlib.metadata, pathlib, re; t=pathlib.Path('requirements.bootstrap.lock.txt').read_text(encoding='utf-8'); e=re.search(r'(?m)^pip==([^ \\\r\n]+)',t).group(1); a=importlib.metadata.version('pip'); assert a==e,(a,e); print('pip OK:',a)"
if errorlevel 1 goto :install_error

echo [5/6] Instalando dependencias Windows hash-verificadas SOLO como wheels...
".venv\Scripts\python.exe" -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.windows.lock.txt
if errorlevel 1 goto :install_error

echo [6/6] Verificando dependencias e imports criticos...
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto :install_error
".venv\Scripts\python.exe" -c "import pandas,numpy,scipy,fastapi,uvicorn,requests,httpx; print('IMPORTS OK | pandas',pandas.__version__,'| numpy',numpy.__version__)"
if errorlevel 1 goto :install_error

echo.
echo ==============================================================
echo INSTALACION WINDOWS COMPLETA - PYTHON %ITMQ_PYTHON% AISLADO
echo ==============================================================
echo Ahora ejecuta CONFIGURAR_WEB.bat y luego INICIAR_WEB.bat.
echo.
pause
exit /b 0

:no_launcher
echo ERROR: No encuentro Python Launcher ^(py.exe^).
echo Instala Python %ITMQ_PYTHON% x64 desde python.org y marca "py launcher".
echo NO instales Visual Studio para compilar pandas.
pause
exit /b 1

:no_py312
echo ERROR: ITM QUANT necesita Python %ITMQ_PYTHON% x64 exactamente en Windows.
echo Otra version de Python no se usara para este hotfix Windows reproducible.
echo Instala Python %ITMQ_PYTHON% x64 y vuelve a ejecutar INSTALAR_WEB.bat.
echo Descarga oficial: https://www.python.org/downloads/release/python-31210/
echo Puedes comprobarlo con: py -0p
pause
exit /b 1

:install_error
echo.
echo ERROR DURANTE LA INSTALACION AISLADA.
echo No se modificaron tus otros programas.
echo Copia las ultimas lineas del error si necesitas soporte.
pause
exit /b 1
