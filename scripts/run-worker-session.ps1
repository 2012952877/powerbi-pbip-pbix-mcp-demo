[CmdletBinding()]
param(
    [string]$Root = 'C:\PBIPMCP',
    [string]$DesktopExe = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe',
    [ValidateRange(0, 60)][int]$ReviewSeconds = 0,
    [ValidateRange(60, 7200)][int]$IdleTimeout = 1800
)
$ErrorActionPreference = 'Stop'
if ([Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0 -or
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq 'S-1-5-18') {
    throw 'Use the dedicated normal-user interactive task, never SYSTEM or Session 0.'
}
$Runtime = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
$Stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,6)
$Run = Join-Path $Root "runs\worker-$Stamp"
New-Item -ItemType Directory -Path $Run -ErrorAction Stop | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
# Windows PowerShell 5.1 otherwise treats ordinary native stderr logging as a terminating error.
$ErrorActionPreference = 'Continue'
& $Runtime -m pbip_mcp.worker --data-dir (Join-Path $Root 'data') --desktop-exe $DesktopExe --max-jobs 0 --idle-timeout $IdleTimeout --review-seconds $ReviewSeconds 1> (Join-Path $Run 'stdout.log') 2> (Join-Path $Run 'stderr.log')
$Code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
exit $Code
