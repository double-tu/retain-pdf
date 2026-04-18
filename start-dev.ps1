$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunDir = Join-Path $RootDir ".run"
$FrontendDir = Join-Path $RootDir "frontend"
$BackendDir = Join-Path $RootDir "backend/rust_api"
$AuthFile = Join-Path $BackendDir "auth.local.json"
$FrontendRuntimeLocal = Join-Path $FrontendDir "runtime-config.local.js"
$BackendLog = Join-Path $RunDir "backend.log"
$FrontendLog = Join-Path $RunDir "frontend.log"
$BackendPidFile = Join-Path $RunDir "backend.pid"
$FrontendPidFile = Join-Path $RunDir "frontend.pid"

$ApiHost = if ($env:API_HOST) { $env:API_HOST } else { "127.0.0.1" }
$ApiPort = if ($env:API_PORT) { $env:API_PORT } else { "41000" }
$SimplePort = if ($env:SIMPLE_PORT) { $env:SIMPLE_PORT } else { "42000" }
$FrontendHost = if ($env:FRONTEND_HOST) { $env:FRONTEND_HOST } else { "127.0.0.1" }
$FrontendPort = if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { "8080" }
$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$TypstBin = if ($env:TYPST_BIN) { $env:TYPST_BIN } else { "typst" }
$UploadMaxMb = if ($env:UPLOAD_MAX_MB) { [int]$env:UPLOAD_MAX_MB } else { 500 }
$UploadMaxPages = if ($env:UPLOAD_MAX_PAGES) { [int]$env:UPLOAD_MAX_PAGES } else { 500 }
$RustApiUploadMaxBytes = if ($env:RUST_API_UPLOAD_MAX_BYTES) { [int64]$env:RUST_API_UPLOAD_MAX_BYTES } else { [int64]$UploadMaxMb * 1024 * 1024 }
$RustApiUploadMaxPages = if ($env:RUST_API_UPLOAD_MAX_PAGES) { [int]$env:RUST_API_UPLOAD_MAX_PAGES } else { $UploadMaxPages }

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

function Require-Command {
    param([string]$CommandName)

    if (-not (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        throw "missing required command: $CommandName"
    }
}

function Test-TcpListening {
    param([int]$Port)

    try {
        $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop
        return $listeners.Count -gt 0
    } catch {
        return $false
    }
}

function Start-LoggedProcess {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$LogPath,
        [hashtable]$EnvironmentVariables = @{}
    )

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $FilePath
    $StartInfo.WorkingDirectory = $WorkingDirectory
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true
    foreach ($Argument in $ArgumentList) {
        [void]$StartInfo.ArgumentList.Add([string]$Argument)
    }
    foreach ($Key in $EnvironmentVariables.Keys) {
        $StartInfo.Environment[$Key] = [string]$EnvironmentVariables[$Key]
    }

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    $Process.Start() | Out-Null

    $OutputWriter = [System.IO.StreamWriter]::new($LogPath, $false, [System.Text.UTF8Encoding]::new($false))
    $OutputHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($Sender, $Args)
        if ($null -ne $Args.Data) {
            $OutputWriter.WriteLine($Args.Data)
            $OutputWriter.Flush()
        }
    }
    $ErrorHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($Sender, $Args)
        if ($null -ne $Args.Data) {
            $OutputWriter.WriteLine($Args.Data)
            $OutputWriter.Flush()
        }
    }
    $CleanupHandler = [System.EventHandler]{
        param($Sender, $Args)
        $OutputWriter.Dispose()
    }

    $Process.add_OutputDataReceived($OutputHandler)
    $Process.add_ErrorDataReceived($ErrorHandler)
    $Process.add_Exited($CleanupHandler)
    $Process.EnableRaisingEvents = $true
    $Process.BeginOutputReadLine()
    $Process.BeginErrorReadLine()
    return $Process
}

function Stop-ProcessIfRunning {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    if (-not $Process.HasExited) {
        $Process.Kill()
        $Process.WaitForExit()
    }
}

Require-Command "cargo"
Require-Command $PythonBin
Require-Command "npm"
Require-Command $TypstBin

if (-not (Test-Path $AuthFile)) {
    throw "missing $AuthFile. Copy backend/rust_api/auth.local.example.json to backend/rust_api/auth.local.json first."
}

$AuthJson = Get-Content -Raw -Encoding UTF8 $AuthFile | ConvertFrom-Json
$ApiKey = ""
if ($AuthJson.api_keys -and $AuthJson.api_keys.Count -gt 0) {
    $ApiKey = [string]$AuthJson.api_keys[0]
}

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "no api_keys found in $AuthFile"
}

$RuntimeConfig = @"
window.__FRONT_RUNTIME_CONFIG__ = {
  ...(window.__FRONT_RUNTIME_CONFIG__ || {}),
  apiBase: "http://$ApiHost`:$ApiPort",
  xApiKey: "$ApiKey",
  mineruToken: "",
  modelApiKey: "",
  model: "",
  baseUrl: "",
  frontMaxBytes: $RustApiUploadMaxBytes,
  frontMaxPageCount: $RustApiUploadMaxPages,
};
"@
Set-Content -Path $FrontendRuntimeLocal -Value $RuntimeConfig -Encoding UTF8

$FrontendDependency = Join-Path $FrontendDir "node_modules/pdfjs-dist/build/pdf.mjs"
if (-not (Test-Path $FrontendDependency)) {
    Write-Host "frontend dependency missing: node_modules/pdfjs-dist/build/pdf.mjs"
    Write-Host "installing frontend dependencies with npm install..."
    Push-Location $FrontendDir
    try {
        npm install
    } finally {
        Pop-Location
    }
}

if (Test-TcpListening -Port ([int]$ApiPort)) {
    throw "port $ApiPort is already in use"
}

if (Test-TcpListening -Port ([int]$FrontendPort)) {
    throw "port $FrontendPort is already in use"
}

Write-Host "writing logs to:"
Write-Host "  backend:  $BackendLog"
Write-Host "  frontend: $FrontendLog"
Write-Host "  typst:    $TypstBin"
Write-Host "  limits:   $RustApiUploadMaxBytes bytes / $RustApiUploadMaxPages pages"

$BackendProcess = $null
$FrontendProcess = $null

try {
    $BackendEnv = @{
        RUST_API_BIND_HOST = $ApiHost
        RUST_API_PORT = [string]$ApiPort
        RUST_API_SIMPLE_PORT = [string]$SimplePort
        TYPST_BIN = $TypstBin
        RUST_API_UPLOAD_MAX_BYTES = [string]$RustApiUploadMaxBytes
        RUST_API_UPLOAD_MAX_PAGES = [string]$RustApiUploadMaxPages
    }

    $BackendProcess = Start-LoggedProcess `
        -FilePath "cargo" `
        -ArgumentList @("run") `
        -WorkingDirectory $BackendDir `
        -LogPath $BackendLog `
        -EnvironmentVariables $BackendEnv
    $BackendProcess.Id | Set-Content -Path $BackendPidFile -Encoding ASCII

    for ($i = 0; $i -lt 60; $i++) {
        if (Test-TcpListening -Port ([int]$ApiPort)) {
            break
        }
        if ($BackendProcess.HasExited) {
            throw "backend exited unexpectedly, check $BackendLog"
        }
        Start-Sleep -Seconds 1
    }

    if (-not (Test-TcpListening -Port ([int]$ApiPort))) {
        throw "backend did not become ready on port $ApiPort, check $BackendLog"
    }

    $FrontendProcess = Start-LoggedProcess `
        -FilePath $PythonBin `
        -ArgumentList @("-m", "http.server", $FrontendPort, "--bind", $FrontendHost) `
        -WorkingDirectory $FrontendDir `
        -LogPath $FrontendLog
    $FrontendProcess.Id | Set-Content -Path $FrontendPidFile -Encoding ASCII

    for ($i = 0; $i -lt 15; $i++) {
        if (Test-TcpListening -Port ([int]$FrontendPort)) {
            break
        }
        if ($FrontendProcess.HasExited) {
            throw "frontend exited unexpectedly, check $FrontendLog"
        }
        Start-Sleep -Seconds 1
    }

    if (-not (Test-TcpListening -Port ([int]$FrontendPort))) {
        throw "frontend did not become ready on port $FrontendPort, check $FrontendLog"
    }
} catch {
    Stop-ProcessIfRunning -Process $FrontendProcess
    Stop-ProcessIfRunning -Process $BackendProcess
    if (Test-Path $FrontendPidFile) {
        Remove-Item -Force $FrontendPidFile
    }
    if (Test-Path $BackendPidFile) {
        Remove-Item -Force $BackendPidFile
    }
    throw
}

Write-Host ""
Write-Host "RetainPDF is running:"
Write-Host "  frontend: http://$FrontendHost`:$FrontendPort"
Write-Host "  backend:  http://$ApiHost`:$ApiPort/health"
Write-Host ""
Write-Host "use .\stop-dev.ps1 to stop both services"
