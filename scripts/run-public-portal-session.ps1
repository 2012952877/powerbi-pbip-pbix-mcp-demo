[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^https://[a-z0-9-]+\.azurewebsites\.net$')]
    [string]$PublicOrigin,
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^(?:\d{1,3}\.){3}\d{1,3}$')][string]$BindAddress
)
$ErrorActionPreference = 'Stop'
$App = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $App '.venv\Scripts\python.exe'
$AuthRoot = 'C:\PBIPMCP\pilot-auth-bidir'
$Logs = Join-Path $AuthRoot 'public-portal-runs'
if (-not (Test-Path -LiteralPath $Logs)) { New-Item -ItemType Directory -Path $Logs | Out-Null }
$Run = Join-Path $Logs ((Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Run | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
# Native uvicorn diagnostics go to bounded-scope private run logs, not a request access log.
$ErrorActionPreference = 'Continue'
& $Runtime (Join-Path $PSScriptRoot 'run-public-portal.py') `
    --owned-pid-file (Join-Path $AuthRoot 'public-portal.pid') `
    --data-dir C:\PBIPMCP\data --auth-config (Join-Path $AuthRoot 'users.json') `
    --host $BindAddress --port 8765 --public-origin $PublicOrigin --allow-public-https `
    1> (Join-Path $Run 'stdout.log') 2> (Join-Path $Run 'stderr.log')
exit $LASTEXITCODE
