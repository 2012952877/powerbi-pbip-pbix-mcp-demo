[CmdletBinding()]
param(
    [string]$Root = 'C:\PBIPMCP',
    [string]$DesktopExe = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe',
    [string]$OutputDirectory = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($Identity.User.Value -eq 'S-1-5-18' -or [Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) {
    throw 'Do not run this script from SYSTEM, a service or Azure Run Command. Use a logged-on normal-user interactive task/session.'
}
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Runtime -PathType Leaf)) { throw 'Run setup.ps1 first.' }
if (-not $OutputDirectory) { throw 'The normal user has no Desktop directory.' }

$Stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$RunDirectory = Join-Path $Root "runs\$Stamp"
New-Item -ItemType Directory -Path $RunDirectory | Out-Null
$DataDirectory = Join-Path $Root 'data'
$Output = Join-Path $OutputDirectory "Synthetic-converted-$Stamp.pbix"
$Verification = Join-Path $RunDirectory 'verification.json'
$Result = Join-Path $RunDirectory 'e2e-result.json'
$WorkerOut = Join-Path $RunDirectory 'worker.stdout.log'
$WorkerErr = Join-Path $RunDirectory 'worker.stderr.log'
$ClientOut = Join-Path $RunDirectory 'client.stdout.log'
$ClientErr = Join-Path $RunDirectory 'client.stderr.log'

& $Runtime -m pbip_mcp.worker --data-dir $DataDirectory --desktop-exe $DesktopExe --probe
if ($LASTEXITCODE -ne 0) { throw 'The interactive desktop is not ready. Leave the session connected and unlocked.' }

$WorkerArguments = "-m pbip_mcp.worker --data-dir `"$DataDirectory`" --desktop-exe `"$DesktopExe`" --max-jobs 1 --idle-timeout 120"
$Worker = Start-Process -FilePath $Runtime -ArgumentList $WorkerArguments -WorkingDirectory $ProjectRoot -PassThru -RedirectStandardOutput $WorkerOut -RedirectStandardError $WorkerErr
$Client = $null
$ClientExit = 2
try {
    $SampleZip = Join-Path $ProjectRoot 'sample\Synthetic.zip'
    $ClientArguments = "-m pbip_mcp.client --data-dir `"$DataDirectory`" --result-json `"$Result`" convert --zip `"$SampleZip`" --output `"$Output`" --verification `"$Verification`" --timeout 900 --wait-ready 30"
    $Client = Start-Process -FilePath $Runtime -ArgumentList $ClientArguments -WorkingDirectory $ProjectRoot -PassThru -RedirectStandardOutput $ClientOut -RedirectStandardError $ClientErr
    if (-not $Client.WaitForExit(1000000)) { throw 'MCP smoke client exceeded its supervised deadline.' }
    $ClientExit = $Client.ExitCode
    if (-not $Worker.WaitForExit(15000)) {
        throw 'The owned worker did not exit within the post-client deadline.'
    }
}
finally {
    if ($null -ne $Client) {
        if (-not $Client.HasExited) { Stop-Process -Id $Client.Id -ErrorAction Stop; $Client.WaitForExit() }
        $Client.Dispose()
    }
    if (-not $Worker.HasExited) {
        Stop-Process -Id $Worker.Id -ErrorAction Stop
        $Worker.WaitForExit()
    }
    $Worker.Dispose()
}

if (Test-Path -LiteralPath $Result) {
    Get-Content -LiteralPath $Result -Raw
}
else {
    Write-Error "No E2E result was written. Inspect the owned run directory: $RunDirectory"
}
exit $ClientExit
