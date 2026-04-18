@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "RootDir=%~dp0"
set "RunDir=%RootDir%.run"
set "FrontendDir=%RootDir%frontend"
set "BackendDir=%RootDir%backend\rust_api"
set "AuthFile=%BackendDir%\auth.local.json"
set "FrontendRuntimeLocal=%FrontendDir%\runtime-config.local.js"
set "BackendLog=%RunDir%\backend.log"
set "FrontendLog=%RunDir%\frontend.log"
set "BackendPidFile=%RunDir%\backend.pid"
set "FrontendPidFile=%RunDir%\frontend.pid"
set "BackendCmd=%RunDir%\backend-dev.cmd"
set "FrontendCmd=%RunDir%\frontend-dev.cmd"
set "ApiKeyFile=%RunDir%\api-key.tmp"

if not defined API_HOST set "API_HOST=127.0.0.1"
if not defined API_PORT set "API_PORT=41000"
if not defined SIMPLE_PORT set "SIMPLE_PORT=42000"
if not defined FRONTEND_HOST set "FRONTEND_HOST=127.0.0.1"
if not defined FRONTEND_PORT set "FRONTEND_PORT=8080"
if not defined PYTHON_BIN set "PYTHON_BIN=python"
if not defined UPLOAD_MAX_MB set "UPLOAD_MAX_MB=500"
if not defined UPLOAD_MAX_PAGES set "UPLOAD_MAX_PAGES=500"
if not defined RUST_API_UPLOAD_MAX_PAGES set "RUST_API_UPLOAD_MAX_PAGES=%UPLOAD_MAX_PAGES%"

if not exist "%RunDir%" mkdir "%RunDir%"

call :RequireCommand cargo || exit /b 1
call :RequireCommand node || exit /b 1
call :RequireCommand npm || exit /b 1
call :RequireCommand "%PYTHON_BIN%" || exit /b 1

if not defined TYPST_BIN call :FindTypst
if not defined TYPST_BIN (
  echo typst not found
  echo install Typst first, or point TYPST_BIN to typst.exe before starting:
  echo   set "TYPST_BIN=C:\path\to\typst.exe"
  echo   start-dev.bat
  echo for winget users, try:
  echo   winget install Typst.Typst
  exit /b 1
)
call :RequireCommand "%TYPST_BIN%" || exit /b 1

if not defined RUST_API_UPLOAD_MAX_BYTES (
  for /f "usebackq delims=" %%A in (`node -e "const mb=BigInt(process.argv[1]||500);process.stdout.write(String(mb*1024n*1024n));" "%UPLOAD_MAX_MB%"`) do set "RUST_API_UPLOAD_MAX_BYTES=%%A"
)

if not exist "%AuthFile%" (
  echo missing %AuthFile%. Copy backend\rust_api\auth.local.example.json to backend\rust_api\auth.local.json first.
  exit /b 1
)

node -e "try{const fs=require('fs');const p=process.argv[1];const j=JSON.parse(fs.readFileSync(p,'utf8'));const k=j.api_keys&&j.api_keys[0];if(!k||!String(k).trim()){console.error('no api_keys found in '+p);process.exit(2)}process.stdout.write(String(k));}catch(e){console.error(e.message);process.exit(1)}" "%AuthFile%" > "%ApiKeyFile%"
if errorlevel 1 (
  if exist "%ApiKeyFile%" del /f /q "%ApiKeyFile%" >nul 2>nul
  exit /b 1
)
set /p "ApiKey="<"%ApiKeyFile%"
del /f /q "%ApiKeyFile%" >nul 2>nul

node -e "const fs=require('fs');const [out,apiHost,apiPort,apiKey,maxBytes,maxPages]=process.argv.slice(1);const apiBase='http://'+apiHost+':'+apiPort;const text='window.__FRONT_RUNTIME_CONFIG__ = {\n  ...(window.__FRONT_RUNTIME_CONFIG__ || {}),\n  apiBase: '+JSON.stringify(apiBase)+',\n  xApiKey: '+JSON.stringify(apiKey)+',\n  mineruToken: \"\",\n  modelApiKey: \"\",\n  model: \"\",\n  baseUrl: \"\",\n  frontMaxBytes: '+maxBytes+',\n  frontMaxPageCount: '+maxPages+',\n};\n';fs.writeFileSync(out,text,'utf8');" "%FrontendRuntimeLocal%" "%API_HOST%" "%API_PORT%" "%ApiKey%" "%RUST_API_UPLOAD_MAX_BYTES%" "%RUST_API_UPLOAD_MAX_PAGES%"
if errorlevel 1 exit /b 1

if not exist "%FrontendDir%\node_modules\pdfjs-dist\build\pdf.mjs" (
  echo frontend dependency missing: node_modules\pdfjs-dist\build\pdf.mjs
  echo installing frontend dependencies with npm install...
  pushd "%FrontendDir%" || exit /b 1
  call npm install
  if errorlevel 1 (
    popd
    exit /b 1
  )
  popd
)

call :IsPortListening "%API_PORT%"
if not errorlevel 1 (
  echo port %API_PORT% is already in use
  exit /b 1
)

call :IsPortListening "%FRONTEND_PORT%"
if not errorlevel 1 (
  echo port %FRONTEND_PORT% is already in use
  exit /b 1
)

echo writing logs to:
echo   backend:  %BackendLog%
echo   frontend: %FrontendLog%
echo   typst:    %TYPST_BIN%
echo   limits:   %RUST_API_UPLOAD_MAX_BYTES% bytes / %RUST_API_UPLOAD_MAX_PAGES% pages

> "%BackendCmd%" (
  echo @echo off
  echo cd /d "%BackendDir%"
  echo set "RUST_API_BIND_HOST=%API_HOST%"
  echo set "RUST_API_PORT=%API_PORT%"
  echo set "RUST_API_SIMPLE_PORT=%SIMPLE_PORT%"
  echo set "TYPST_BIN=%TYPST_BIN%"
  echo set "RUST_API_UPLOAD_MAX_BYTES=%RUST_API_UPLOAD_MAX_BYTES%"
  echo set "RUST_API_UPLOAD_MAX_PAGES=%RUST_API_UPLOAD_MAX_PAGES%"
  echo cargo run ^> "%BackendLog%" 2^>^&1
)

> "%FrontendCmd%" (
  echo @echo off
  echo cd /d "%FrontendDir%"
  echo "%PYTHON_BIN%" -m http.server %FRONTEND_PORT% --bind "%FRONTEND_HOST%" ^> "%FrontendLog%" 2^>^&1
)

start "RetainPDF backend" /min "%BackendCmd%"

for /l %%I in (1,1,60) do (
  call :IsPortListening "%API_PORT%"
  if not errorlevel 1 goto :BackendReady
  timeout /t 1 /nobreak >nul
)

echo backend did not become ready on port %API_PORT%, check %BackendLog%
call :StopByPidFile "backend" "%BackendPidFile%" >nul 2>nul
exit /b 1

:BackendReady
call :WriteListeningPid "%API_PORT%" "%BackendPidFile%"

start "RetainPDF frontend" /min "%FrontendCmd%"

for /l %%I in (1,1,15) do (
  call :IsPortListening "%FRONTEND_PORT%"
  if not errorlevel 1 goto :FrontendReady
  timeout /t 1 /nobreak >nul
)

echo frontend did not become ready on port %FRONTEND_PORT%, check %FrontendLog%
call :StopByPidFile "frontend" "%FrontendPidFile%" >nul 2>nul
call :StopByPidFile "backend" "%BackendPidFile%" >nul 2>nul
exit /b 1

:FrontendReady
call :WriteListeningPid "%FRONTEND_PORT%" "%FrontendPidFile%"

echo.
echo RetainPDF is running:
echo   frontend: http://%FRONTEND_HOST%:%FRONTEND_PORT%
echo   backend:  http://%API_HOST%:%API_PORT%/health
echo.
echo use .\stop-dev.bat to stop both services
exit /b 0

:RequireCommand
if exist "%~1" exit /b 0
where "%~1" >nul 2>nul
if errorlevel 1 (
  echo missing required command: %~1
  exit /b 1
)
exit /b 0

:FindTypst
for /f "delims=" %%P in ('where typst 2^>nul') do (
  set "TYPST_BIN=%%P"
  exit /b 0
)
if exist "%RootDir%backend\typst-win32\bin\typst.exe" (
  set "TYPST_BIN=%RootDir%backend\typst-win32\bin\typst.exe"
  exit /b 0
)
if exist "%RootDir%desktop\app\backend\typst\bin\typst.exe" (
  set "TYPST_BIN=%RootDir%desktop\app\backend\typst\bin\typst.exe"
  exit /b 0
)
if exist "%RootDir%typst\bin\typst.exe" (
  set "TYPST_BIN=%RootDir%typst\bin\typst.exe"
  exit /b 0
)
exit /b 0

:IsPortListening
netstat -ano -p tcp | findstr /R /C:":%~1 .*LISTENING" >nul 2>nul
exit /b %ERRORLEVEL%

:WriteListeningPid
set "FoundPid="
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%~1 .*LISTENING"') do if not defined FoundPid set "FoundPid=%%P"
if not defined FoundPid (
  echo could not find process listening on port %~1
  exit /b 1
)
> "%~2" echo %FoundPid%
exit /b 0

:StopByPidFile
if not exist "%~2" exit /b 0
set "StopPid="
set /p "StopPid="<"%~2"
if defined StopPid taskkill /PID %StopPid% /T /F >nul 2>nul
del /f /q "%~2" >nul 2>nul
exit /b 0
