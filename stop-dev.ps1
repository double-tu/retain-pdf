$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunDir = Join-Path $RootDir ".run"
$BackendPidFile = Join-Path $RunDir "backend.pid"
$FrontendPidFile = Join-Path $RunDir "frontend.pid"

function Stop-ByPidFile {
    param(
        [string]$Name,
        [string]$PidFile
    )

    if (-not (Test-Path $PidFile)) {
        Write-Host "$Name is not running (missing $(Split-Path -Leaf $PidFile))"
        return
    }

    $Pid = (Get-Content -Raw -Encoding ASCII $PidFile).Trim()
    if ([string]::IsNullOrWhiteSpace($Pid)) {
        Remove-Item -Force $PidFile
        Write-Host "$Name pid file was empty and has been removed"
        return
    }

    $Process = Get-Process -Id ([int]$Pid) -ErrorAction SilentlyContinue
    if ($null -ne $Process) {
        Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 500
            $Process = Get-Process -Id ([int]$Pid) -ErrorAction SilentlyContinue
            if ($null -eq $Process) {
                break
            }
        }
        if ($null -ne $Process) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
        Write-Host "stopped $Name (pid $Pid)"
    } else {
        Write-Host "$Name process $Pid was not running"
    }

    Remove-Item -Force $PidFile
}

Stop-ByPidFile -Name "backend" -PidFile $BackendPidFile
Stop-ByPidFile -Name "frontend" -PidFile $FrontendPidFile
