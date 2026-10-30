$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$workspaceRoot = Split-Path $projectRoot -Parent
$runtimeRoot = Join-Path $workspaceRoot '.runtime'
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
$nodeMarker = Join-Path $runtimeRoot 'node-path.txt'
if (Get-Command node -ErrorAction SilentlyContinue) {
    $nodeDirectory = Split-Path (Get-Command node).Source
} elseif (Test-Path -LiteralPath $nodeMarker) {
    $nodeDirectory = (Get-Content -LiteralPath $nodeMarker -Raw).Trim()
} else {
    $nodeVersion = 'v24.22.1'
    $archiveName = "node-$nodeVersion-win-x64.zip"
    $archivePath = Join-Path $runtimeRoot $archiveName
    Write-Host 'Downloading portable Node.js into the workspace...'
    Invoke-WebRequest "https://nodejs.org/dist/$nodeVersion/$archiveName" -OutFile $archivePath
    $checksums = (Invoke-WebRequest "https://nodejs.org/dist/$nodeVersion/SHASUMS256.txt").Content
    $checksumLine = ($checksums -split "`n" | Where-Object { $_.TrimEnd().EndsWith("  $archiveName") })
    $expectedHash = ($checksumLine -split '\s+')[0]
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLower() -ne $expectedHash) { throw 'Node.js checksum verification failed.' }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeRoot -Force
    $nodeDirectory = Join-Path $runtimeRoot "node-$nodeVersion-win-x64"
    Set-Content -LiteralPath $nodeMarker -Value $nodeDirectory
}
$env:PATH = "$nodeDirectory;$env:PATH"
Push-Location $projectRoot
try {
    & "$nodeDirectory\npm.cmd" ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    python -m pip install --target (Join-Path $runtimeRoot 'python') -r backend/requirements.lock.txt --upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
    python -m pip install --target (Join-Path $runtimeRoot 'yfinance') -r backend/requirements-yahoo.lock.txt --upgrade
    if ($LASTEXITCODE -ne 0) { throw 'Yahoo FX dependency installation failed.' }
    python -I scripts/check-bop.py --offline
    if ($LASTEXITCODE -ne 0) { throw 'Packaged BOP chart verification failed.' }
    & "$nodeDirectory\npm.cmd" run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    Write-Host 'Setup complete. Run .\scripts\start.ps1 to open the application.'
} finally { Pop-Location }
