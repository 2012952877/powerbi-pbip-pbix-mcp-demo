[CmdletBinding()]
param(
    [string]$Root = 'C:\PBIPMCP',
    [ValidateSet('stdio', 'streamable-http')]
    [string]$Transport = 'stdio',
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$AuthConfig,
    [string]$LimitsConfig
)
$ErrorActionPreference = 'Stop'
$Runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
$Options = @()
if ($Transport -eq 'streamable-http' -and -not $AuthConfig) {
    throw 'Authenticated HTTP requires -AuthConfig. Anonymous HTTP is disabled.'
}
if ($AuthConfig) { $Options += @('--auth-config', $AuthConfig) }
if ($LimitsConfig) { $Options += @('--limits-config', $LimitsConfig) }
& $Runtime -m pbip_mcp.server --data-dir (Join-Path $Root 'data') --transport $Transport --port $Port @Options
exit $LASTEXITCODE
