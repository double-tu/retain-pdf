@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "RootDir=%~dp0"
set "RunDir=%RootDir%.run"
set "BackendPidFile=%RunDir%\backend.pid"
set "FrontendPidFile=%RunDir%\frontend.pid"

call :StopByPidFile "backend" "%BackendPidFile%"
call :StopByPidFile "frontend" "%FrontendPidFile%"
exit /b 0

:StopByPidFile
set "Name=%~1"
set "PidFile=%~2"

if not exist "%PidFile%" (
  echo %Name% is not running ^(missing %~nx2^)
  exit /b 0
)

set "Pid="
set /p "Pid="<"%PidFile%"
if not defined Pid (
  del /f /q "%PidFile%" >nul 2>nul
  echo %Name% pid file was empty and has been removed
  exit /b 0
)

call :IsPidRunning "%Pid%"
if errorlevel 1 (
  echo %Name% process %Pid% was not running
  del /f /q "%PidFile%" >nul 2>nul
  exit /b 0
)

taskkill /PID %Pid% /T >nul 2>nul
for /l %%I in (1,1,20) do (
  call :IsPidRunning "%Pid%"
  if errorlevel 1 goto :Stopped
  timeout /t 1 /nobreak >nul
)

taskkill /PID %Pid% /T /F >nul 2>nul

:Stopped
echo stopped %Name% ^(pid %Pid%^)
del /f /q "%PidFile%" >nul 2>nul
exit /b 0

:IsPidRunning
tasklist /FI "PID eq %~1" 2>nul | findstr /R /C:" %~1 " >nul 2>nul
exit /b %ERRORLEVEL%
