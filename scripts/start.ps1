param([switch]$NoBrowser, [ValidateRange(1024,65535)][int]$Port=8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$workspaceRoot = Split-Path $projectRoot -Parent
$runtimeRoot = Join-Path $workspaceRoot '.runtime'
$env:PYTHONPATH = (Join-Path $runtimeRoot 'python') + [IO.Path]::PathSeparator + (Join-Path $runtimeRoot 'yfinance')
$url = "http://127.0.0.1:$Port"
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'dist\index.html'))) { throw 'Run .\scripts\setup.ps1 first.' }
$ready = $false
try { $response = Invoke-RestMethod "$url/api/health" -TimeoutSec 2; $ready = $response.mode -eq 'synthetic-paper' } catch {}
if (-not $ready) {
    $pythonCommand = (Get-Command python).Source
    $serverProcess = Start-Process -FilePath $pythonCommand -ArgumentList @('-m','uvicorn','backend.app:app','--host','127.0.0.1','--port',"$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot 'server.log') -RedirectStandardError (Join-Path $runtimeRoot 'server-error.log') -PassThru
    Set-Content -LiteralPath (Join-Path $runtimeRoot 'server-pid.txt') -Value $serverProcess.Id
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 700
        try { $response = Invoke-RestMethod "$url/api/health" -TimeoutSec 1; if ($response.mode -eq 'synthetic-paper') { $ready = $true; break } } catch {}
        if ($serverProcess.HasExited) { break }
    }
}
if (-not $ready) { throw "The server did not start. Inspect $runtimeRoot\server-error.log." }
Write-Host "Meridian is running at $url"
if (-not $NoBrowser) { Start-Process $url }
