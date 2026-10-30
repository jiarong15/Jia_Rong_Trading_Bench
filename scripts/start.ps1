param([switch]$NoBrowser, [switch]$Restart, [ValidateRange(1024,65535)][int]$Port=8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$launchArguments = @((Join-Path $projectRoot 'launch.py'), '--port', "$Port")
if ($NoBrowser) { $launchArguments += '--no-browser' }
if ($Restart) { $launchArguments += '--restart' }
& python @launchArguments
exit $LASTEXITCODE
