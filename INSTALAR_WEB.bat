@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d %~dp0

set /p ITMQ_VERSION=<VERSION.txt
rem v1.57.1 · EL INTERPRETE SE LEE DE .python-version, NO SE ESCRIBE AQUI.
rem
rem Antes este fichero fijaba 3.12.10 a mano mientras el gate de release exigia
rem 3.12.14 (.python-version). Las dos cosas no podian ser ciertas a la vez: en
rem Windows se instalaba 3.12.10 —porque python.org no distribuye instalador de
rem 3.12.14— y despues el gate rechazaba certificar ese mismo entorno. Leyendo
rem el pin desde el mismo fichero que lee el gate, la contradiccion no se puede
rem reintroducir. La disponibilidad en Windows de la version fijada la declara
rem .python-runtime.json y la comprueba el gate.
set /p ITMQ_PYTHON=<.python-version
for /f "tokens=1,2 delims=." %%a in ("%ITMQ_PYTHON%") do set ITMQ_PYMM=%%a.%%b
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

py -%ITMQ_PYMM% -c "import sys,platform,pathlib; want=pathlib.Path('.python-version').read_text(encoding='utf-8').strip(); got='.'.join(map(str,sys.version_info[:3])); assert got==want,(got,want); assert platform.architecture()[0]=='64bit'; print(sys.version)" >nul 2>nul
if errorlevel 1 goto :no_python

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
  py -%ITMQ_PYMM% -m venv .venv
  if errorlevel 1 goto :install_error
) else (
  echo [1/6] Entorno privado Python %ITMQ_PYTHON% verificado.
)

echo [2/6] Verificando interprete privado exacto...
".venv\Scripts\python.exe" -c "import sys,platform,pathlib; want=pathlib.Path('.python-version').read_text(encoding='utf-8').strip(); got='.'.join(map(str,sys.version_info[:3])); assert got==want,(got,want); assert platform.architecture()[0]=='64bit'; print('Python OK:',got,platform.machine())"
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

:no_python
echo ERROR: ITM QUANT necesita Python %ITMQ_PYTHON% x64 exactamente en Windows.
echo Esa version es la que python.org distribuye como instalador para Windows y
echo es la unica con la que esta release se ha certificado.
echo Instala Python %ITMQ_PYTHON% x64 y vuelve a ejecutar INSTALAR_WEB.bat.
echo Descarga oficial: https://www.python.org/downloads/windows/
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
