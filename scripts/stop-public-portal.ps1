[CmdletBinding()]
param([string]$PidFile = 'C:\PBIPMCP\pilot-auth-bidir\public-portal.pid')
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PidFile)) {
    '{"already_stopped":true}'
    exit 0
}
$Text = (Get-Content -Raw -LiteralPath $PidFile).Trim()
if ($Text -notmatch '^[1-9][0-9]{0,9}$') { throw 'Invalid public portal PID file.' }
$OwnedPid = [int]$Text
$Process = Get-CimInstance Win32_Process -Filter "ProcessId=$OwnedPid"
if ($Process) {
    if ($Process.Name -ne 'python.exe' -or $Process.CommandLine -notlike '*run-public-portal.py*') {
        throw 'The PID was reused; another process will not be stopped.'
    }
    Stop-Process -Id $OwnedPid
    Wait-Process -Id $OwnedPid -Timeout 15 -ErrorAction SilentlyContinue
    $Remaining = Get-Process -Id $OwnedPid -ErrorAction SilentlyContinue
    if ($Remaining -and -not $Remaining.HasExited) { throw 'Public portal is still exiting.' }
}
if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile }
[ordered]@{public_portal_stopped=$true;pid=$OwnedPid;worker_stopped=$false} | ConvertTo-Json -Compress
