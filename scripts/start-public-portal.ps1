[CmdletBinding()]
param(
    [string]$Root = 'C:\PBIPMCP',
    [Parameter(Mandatory=$true)][string]$PublicOrigin,
    [string]$AuthConfig = 'C:\PBIPMCP\pilot-auth-bidir\users.json',
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^(?:\d{1,3}\.){3}\d{1,3}$')][string]$BindAddress
)
$ErrorActionPreference = 'Stop'
$Runtime = Join-Path $Root 'app\.venv\Scripts\python.exe'
$Script = Join-Path $PSScriptRoot 'run-public-portal.py'
$PidFile = Join-Path (Split-Path -Parent $AuthConfig) 'public-portal.pid'
if (-not (Test-Path -LiteralPath $Runtime -PathType Leaf)) { throw 'Install the approved candidate first.' }
& $Runtime $Script --owned-pid-file $PidFile --data-dir (Join-Path $Root 'data') `
    --auth-config $AuthConfig --host $BindAddress --port 8765 `
    --public-origin $PublicOrigin --allow-public-https
if ($LASTEXITCODE -ne 0) { throw 'The public-origin portal stopped with an error.' }
