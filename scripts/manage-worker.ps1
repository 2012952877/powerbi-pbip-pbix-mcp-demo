[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Register','Start','Stop','Status','Doctor')][string]$Action,
    [string]$Root = 'C:\PBIPMCP',
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$WorkerUser = 'pbipdemo',
    [string]$DesktopExe = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe',
    [ValidateRange(0,60)][int]$ReviewSeconds = 0,
    [ValidateRange(1,700)][int]$WaitSeconds = 30
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Project = Split-Path -Parent $PSScriptRoot
$Root = [IO.Path]::GetFullPath($Root)
if ($Root.TrimEnd('\') -eq [IO.Path]::GetPathRoot($Root).TrimEnd('\')) { throw 'A drive root is not allowed.' }
$Runtime = Join-Path $Project '.venv\Scripts\python.exe'
$Data = Join-Path $Root 'data'
$TaskName = 'PBIPMCP-Worker'
$Script = Join-Path $PSScriptRoot 'run-worker-session.ps1'
$Execute = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$BaseArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Root `"$Root`" -DesktopExe `"$DesktopExe`""
$Arguments = "$BaseArguments -ReviewSeconds $ReviewSeconds"
$ConfiguredReview = $ReviewSeconds
$ReviewWasRequested = $PSBoundParameters.ContainsKey('ReviewSeconds')
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

function Assert-OwnedTask($Current) {
    $Sid = (New-Object Security.Principal.NTAccount($env:COMPUTERNAME, $WorkerUser)).Translate([Security.Principal.SecurityIdentifier]).Value
    $PrincipalSid = (New-Object Security.Principal.NTAccount($Current.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
    $Match = [regex]::Match($Current.Actions[0].Arguments, '^' + [regex]::Escape($BaseArguments) + ' -ReviewSeconds ([0-9]{1,2})$')
    if ($Sid -ne $PrincipalSid -or $Current.Principal.LogonType -ne 'Interactive' -or
        $Current.Principal.RunLevel -ne 'Limited' -or @($Current.Actions).Count -ne 1 -or
        $Current.Actions[0].Execute -ne $Execute -or -not $Match.Success -or
        $Current.Actions[0].WorkingDirectory -ne $Project) {
        throw 'Existing task does not match this user, project, root and review setting; it was not changed.'
    }
    $SavedReview = [int]$Match.Groups[1].Value
    if ($SavedReview -gt 60) { throw 'The existing task has an invalid review duration.' }
    if ($ReviewWasRequested -and $Action -in @('Register','Start') -and $SavedReview -ne $ReviewSeconds) {
        throw 'Review setting differs from the owned task. Stop and explicitly re-register that task to change configuration.'
    }
    $script:ConfiguredReview = $SavedReview
}

function Get-Health {
    $Json = & $Runtime -m pbip_mcp.client --data-dir $Data status
    if ($LASTEXITCODE) { throw 'The MCP controller status call failed.' }
    return ($Json | ConvertFrom-Json)
}

if (-not (Test-Path $Runtime -PathType Leaf)) { throw 'Missing runtime; run setup.ps1 first.' }
if ($Task) { Assert-OwnedTask $Task }
if ($Action -eq 'Doctor') {
    & $Runtime (Join-Path $PSScriptRoot 'worker-doctor.py') --root $Root --desktop-exe $DesktopExe
    exit $LASTEXITCODE
}
if ($Action -in @('Register','Start') -and -not $Task) {
    if (-not (Test-Path $DesktopExe -PathType Leaf)) { throw 'Power BI Desktop is not installed.' }
    $Principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$WorkerUser" -LogonType Interactive -RunLevel Limited
    $TaskAction = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $Project
    $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Principal $Principal -Settings $Settings | Out-Null
    $Task = Get-ScheduledTask -TaskName $TaskName
    Assert-OwnedTask $Task
}
if ($Action -eq 'Start') {
    if ($Task.State -eq 'Running') {
        $Health = Get-Health
        if (-not $Health.worker.ready) { throw "Task is running but worker is not ready: $($Health.worker.message)" }
        [ordered]@{task=$TaskName;already_running=$true;review_seconds=$ConfiguredReview;worker=$Health.worker} | ConvertTo-Json -Depth 5
        exit 0
    }
    $Health = Get-Health
    if ($Health.worker.ready) { throw 'Another worker is ready for this queue; do not launch a duplicate.' }
    foreach ($Name in @('PBIPMCP-Demo','PBIPMCP-External')) {
        $Other = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($Other -and $Other.State -eq 'Running') { throw "Stop the existing $Name before starting the persistent worker." }
    }
    $Stop = Join-Path $Data 'worker.stop'
    if (Test-Path -LiteralPath $Stop -PathType Leaf) { Remove-Item -LiteralPath $Stop }
    Start-ScheduledTask -TaskName $TaskName
    $Until = (Get-Date).AddSeconds($WaitSeconds)
    do {
        Start-Sleep -Seconds 1
        $Health = Get-Health
        if ($Health.worker.ready) { break }
    } while ((Get-Date) -lt $Until)
    if (-not $Health.worker.ready) { throw "Worker not ready: $($Health.worker.message). Reconnect/unlock its RDP desktop, inspect runs\worker-*, then Start again." }
    [ordered]@{task=$TaskName;already_running=$false;review_seconds=$ConfiguredReview;worker=$Health.worker} | ConvertTo-Json -Depth 5
    exit 0
}
if ($Action -eq 'Stop') {
    if (-not $Task -or $Task.State -ne 'Running') {
        [ordered]@{task=$TaskName;already_stopped=$true} | ConvertTo-Json
        exit 0
    }
    [IO.File]::WriteAllText((Join-Path $Data 'worker.stop'), (Get-Date).ToUniversalTime().ToString('o'))
    $Until = (Get-Date).AddSeconds($WaitSeconds)
    do {
        Start-Sleep -Seconds 1
        $Task = Get-ScheduledTask -TaskName $TaskName
    } while ($Task.State -eq 'Running' -and (Get-Date) -lt $Until)
    if ($Task.State -eq 'Running') { throw 'Graceful stop is pending until the owned conversion finishes; no process was killed.' }
}
$Health = Get-Health
[ordered]@{task=$TaskName;registered=[bool]$Task;state=$(if($Task){[string]$Task.State}else{'Absent'});review_seconds=$ConfiguredReview;worker=$Health.worker} | ConvertTo-Json -Depth 5
