[CmdletBinding()]
param(
    [string]$Root = 'C:\PBIPMCP',
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$WorkerUser = 'pbipdemo',
    [string]$PythonExe = 'C:\Python312\python.exe',
    [string]$DesktopExe = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe',
    [switch]$RegisterInteractiveTask
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Root = [IO.Path]::GetFullPath($Root)
if ($Root.TrimEnd('\') -eq [IO.Path]::GetPathRoot($Root).TrimEnd('\')) {
    throw 'A drive root cannot be used as the demo root.'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Python 3.12 x64 is missing. Install it or supply -PythonExe.'
}
if (-not (Test-Path -LiteralPath $DesktopExe -PathType Leaf)) {
    throw 'Standard x64 Power BI Desktop is missing. Supply -DesktopExe after installation.'
}
@'
import struct
import sys
assert sys.version_info[:2] == (3, 12) and struct.calcsize("P") == 8, "Python 3.12 x64 required"
'@ | & $PythonExe -
if ($LASTEXITCODE -ne 0) { throw 'Python version or architecture is unsupported.' }

$Account = New-Object Security.Principal.NTAccount($env:COMPUTERNAME, $WorkerUser)
$WorkerSid = $Account.Translate([Security.Principal.SecurityIdentifier]).Value
if ($WorkerSid -eq 'S-1-5-18') { throw 'The worker account must not be SYSTEM.' }
New-Item -ItemType Directory -Path $Root -Force | Out-Null
if ((Get-Item -LiteralPath $Root).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'The demo root must be a real local directory, not a reparse point.'
}

& icacls.exe $Root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*${WorkerSid}:(OI)(CI)RX" /Q
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict root ACL.' }
& icacls.exe $Root /remove:g '*S-1-1-0' '*S-1-5-11' '*S-1-5-32-545' /Q
if ($LASTEXITCODE -ne 0) { throw 'Failed to remove broad root access.' }
foreach ($Name in @('data', 'runs', 'pilot')) {
    $Directory = Join-Path $Root $Name
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    & icacls.exe $Directory /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "*${WorkerSid}:(OI)(CI)M" /Q
    if ($LASTEXITCODE -ne 0) { throw "Failed to scope the $Name ACL." }
    & icacls.exe $Directory /remove:g '*S-1-1-0' '*S-1-5-11' '*S-1-5-32-545' /Q
    if ($LASTEXITCODE -ne 0) { throw "Failed to remove broad $Name access." }
}

$Venv = Join-Path $ProjectRoot '.venv'
$Runtime = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Runtime -PathType Leaf)) {
    & $PythonExe -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed.' }
}
& $Runtime -m pip install --quiet -r (Join-Path $ProjectRoot 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Installing the pinned runtime dependencies failed.' }
$SourceRoot = Join-Path $ProjectRoot 'src\pbip_mcp'
$PackageSources = @(Get-ChildItem -LiteralPath $SourceRoot -File -Recurse | Where-Object {
    $_.Extension -in @('.py', '.html') -and $_.FullName -notmatch '\\__pycache__\\'
})
foreach ($Source in $PackageSources) {
    $Relative = $Source.FullName.Substring($SourceRoot.Length + 1)
    $Generated = Join-Path $ProjectRoot ('build\lib\pbip_mcp\' + $Relative)
    if (Test-Path -LiteralPath $Generated -PathType Leaf) {
        Remove-Item -LiteralPath $Generated
    }
}
& $Runtime -m pip install --quiet --no-deps $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'Installing the converter package failed.' }
foreach ($Source in $PackageSources) {
    $Relative = $Source.FullName.Substring($SourceRoot.Length + 1)
    $Installed = Join-Path $Venv ('Lib\site-packages\pbip_mcp\' + $Relative)
    if (-not (Test-Path -LiteralPath $Installed -PathType Leaf) -or
        (Get-FileHash -LiteralPath $Source.FullName).Hash -ne (Get-FileHash -LiteralPath $Installed).Hash) {
        throw "Installed source does not match deployment: $($Source.Name)"
    }
}
@'
from comtypes.client import GetModule
GetModule("UIAutomationCore.dll")
from pywinauto.controls.uiawrapper import UIAWrapper
'@ | & $Runtime -
if ($LASTEXITCODE -ne 0) { throw 'Preparing UI Automation failed. Install the Microsoft Visual C++ v14 x64 Redistributable if win32ui reports a missing DLL.' }

$SampleDirectory = Join-Path $ProjectRoot 'sample\Synthetic'
$SampleZip = Join-Path $ProjectRoot 'sample\Synthetic.zip'
if (-not (Test-Path -LiteralPath $SampleZip)) {
    & $Runtime -m pbip_mcp.synthetic --directory $SampleDirectory --zip $SampleZip
    if ($LASTEXITCODE -ne 0) { throw 'Generating the full synthetic project failed.' }
}

if ($RegisterInteractiveTask) {
    $TaskName = 'PBIPMCP-Demo'
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        throw 'PBIPMCP-Demo already exists. Inspect it before explicitly replacing or removing it.'
    }
    $StartScript = Join-Path $PSScriptRoot 'start-demo.ps1'
    $Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -Root `"$Root`" -DesktopExe `"$DesktopExe`""
    $Action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $Arguments -WorkingDirectory $ProjectRoot
    $Principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$WorkerUser" -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Principal $Principal -Settings $Settings | Out-Null
}

[ordered]@{
    installed = $true
    project = $ProjectRoot
    data = (Join-Path $Root 'data')
    runtime = $Runtime
    desktop = $DesktopExe
    task_registered = [bool]$RegisterInteractiveTask
    next_step = 'Keep the normal user logged on with an unlocked desktop, finish Desktop first-run setup, then run start-demo.ps1 or start PBIPMCP-Demo.'
    desktop_started_by_setup = $false
} | ConvertTo-Json
