[CmdletBinding()]
param(
    [string]$Root = 'C:\PBIPMCP',
    [Parameter(Mandatory=$true)][string]$AuthConfig,
    [ValidateRange(1,65535)][int]$Port = 8765,
    [string]$LimitsConfig
)
$ErrorActionPreference = 'Stop'
$Runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
$Options = @()
if ($LimitsConfig) { $Options += @('--limits-config', $LimitsConfig) }
# Foreground controller only. Ctrl+C stops this owned server without touching Desktop.
& $Runtime -m pbip_mcp.portal --data-dir (Join-Path $Root 'data') --auth-config $AuthConfig --port $Port @Options
exit $LASTEXITCODE
