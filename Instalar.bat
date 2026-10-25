@echo off
setlocal EnableExtensions
title Instalador - Expedientes PJN
cd /d "%~dp0"

if not exist "%~dp0app\pjn_core.py" (
  echo.
  echo  Primero hay que descomprimir la carpeta:
  echo  clic derecho en el archivo ZIP, "Extraer todo", y despues abrir Instalar.bat
  echo  desde la carpeta descomprimida.
  echo.
  pause
  exit /b 1
)

set "DEST=%LOCALAPPDATA%\ExpedientesPJN"
echo.
echo  Instalando "Expedientes PJN"...
echo.

rem --- 1. Python ------------------------------------------------------------
set "PY="
call :buscar_python
if not defined PY (
  echo  No se encontro Python. Se instala con winget, puede pedir confirmacion...
  winget install -e --id Python.Python.3.12 --scope user
  call :buscar_python
)
if not defined PY (
  echo.
  echo  No se pudo instalar Python automaticamente.
  echo  Instalelo desde https://www.python.org/downloads/ y vuelva a ejecutar este instalador.
  pause
  exit /b 1
)
echo  Python: %PY%

rem --- 2. Copiar el programa -------------------------------------------------
if not exist "%DEST%" mkdir "%DEST%"
copy /y "app\*.*" "%DEST%\" >nul

rem --- 3. Entorno y librerias ------------------------------------------------
if not exist "%DEST%\venv\Scripts\python.exe" "%PY%" -m venv "%DEST%\venv"
echo  Instalando librerias, puede tardar unos minutos...
"%DEST%\venv\Scripts\python.exe" -m pip install --upgrade --quiet pip
"%DEST%\venv\Scripts\python.exe" -m pip install --upgrade --quiet playwright keyring customtkinter truststore certifi
if errorlevel 1 (
  echo  Error instalando librerias. Revise la conexion a internet y vuelva a intentar.
  pause
  exit /b 1
)

rem --- 4. Navegador: se usa Edge; si no esta, se baja Chromium ---------------
set "EDGE="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=1"
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=1"
if not defined EDGE "%DEST%\venv\Scripts\python.exe" -m playwright install chromium

rem --- 5. Acceso directo en el Escritorio ------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Expedientes PJN.lnk')); $s.TargetPath='%DEST%\venv\Scripts\pythonw.exe'; $s.Arguments='\"%DEST%\lanzador.pyw\"'; $s.WorkingDirectory='%DEST%'; $s.IconLocation='%DEST%\icono.ico'; $s.Description='Descargar expedientes del PJN'; $s.Save()"

echo.
echo  Listo. En el Escritorio quedo el icono "Expedientes PJN".
echo.
pause
exit /b 0

:buscar_python
for %%V in (315 314 313 312 311 310) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
  if not defined PY if exist "%ProgramFiles%\Python%%V\python.exe" set "PY=%ProgramFiles%\Python%%V\python.exe"
)
if not defined PY (
  for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
)
exit /b 0
