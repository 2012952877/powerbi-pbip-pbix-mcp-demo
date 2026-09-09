[CmdletBinding()]
param(
    [string]$Root = 'C:\PBIPMCP',
    [string]$DesktopExe = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe'
)
$ErrorActionPreference = 'Stop'
$Runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
& $Runtime -m pbip_mcp.worker --data-dir (Join-Path $Root 'data') --desktop-exe $DesktopExe
exit $LASTEXITCODE
