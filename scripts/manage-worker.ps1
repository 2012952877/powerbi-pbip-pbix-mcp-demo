[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Register','Configure','Start','Stop','Status','Doctor')][string]$Action,
    [string]$Root = 'C:\PBIPMCP',
    [ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$WorkerUser = 'pbipdemo',
    [string]$DesktopExe = 'C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe',
    [ValidateRange(0,60)][int]$ReviewSeconds = 0,
    [ValidateScript({ $_ -eq 0 -or ($_ -ge 60 -and $_ -le 7200) })][int]$IdleTimeout = 1800,
    [ValidateSet('Manual','SessionAware')][string]$RecoveryMode = 'Manual',
    [ValidateRange(1,700)][int]$WaitSeconds = 30
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Project = Split-Path -Parent $PSScriptRoot
$Root = [IO.Path]::GetFullPath($Root)
if ($Root.TrimEnd('\') -eq [IO.Path]::GetPathRoot($Root).TrimEnd('\')) { throw 'A drive root is not allowed.' }
$Runtime = Join-Path $Project '.venv\Scripts\python.exe'
$Data = Join-Path $Root 'data'
$Stop = Join-Path $Data 'worker.stop'
$TaskName = 'PBIPMCP-Worker'
$Script = Join-Path $PSScriptRoot 'run-worker-session.ps1'
$Execute = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$BaseArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Root `"$Root`" -DesktopExe `"$DesktopExe`""
$ConfiguredReview = $ReviewSeconds
$ConfiguredIdleTimeout = $IdleTimeout
$ConfiguredRecoveryMode = if ($RecoveryMode -eq 'SessionAware') { 'SessionAware' } else { 'Manual' }
$ReviewWasRequested = $PSBoundParameters.ContainsKey('ReviewSeconds')
$IdleWasRequested = $PSBoundParameters.ContainsKey('IdleTimeout')
$RecoveryWasRequested = $PSBoundParameters.ContainsKey('RecoveryMode')
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

function Resolve-TaskUserSid([string]$UserId) {
    # Task Scheduler may persist the same account as a name or a SID.
    if ($UserId -match '\AS-[0-9]+(?:-[0-9]+)+\z') { return ([Security.Principal.SecurityIdentifier]$UserId).Value }
    return (New-Object Security.Principal.NTAccount($UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
}

function Assert-RecoveryProfile($Current, $Mode, $SavedIdle, $Sid) {
    $Settings = $Current.Settings
    # Native Task Scheduler returns $null for <Triggers/>; @($null) would count as one.
    $Triggers = @()
    if ($null -ne $Current.Triggers) { $Triggers = @($Current.Triggers) }
    if ($Mode -eq 'SessionAware') {
        if ($SavedIdle -ne 0 -or $Settings.ExecutionTimeLimit -ne 'PT0S' -or
            $Settings.MultipleInstances -ne 'IgnoreNew' -or $Settings.RestartCount -ne 3 -or
            $Settings.RestartInterval -ne 'PT1M' -or $Triggers.Count -ne 1) {
            throw 'The existing SessionAware task has an unowned recovery profile; it was not changed.'
        }
        $Trigger = $Triggers[0]
        if ($Trigger.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' -or
            -not $Trigger.Enabled -or -not $Trigger.UserId -or
            $Trigger.StartBoundary -or $Trigger.EndBoundary -or
            $Trigger.Delay -notin @($null, '', 'PT0S') -or
            $Trigger.ExecutionTimeLimit -notin @($null, '', 'PT0S') -or
            $Trigger.Repetition.Interval -notin @($null, '', 'PT0S') -or
            $Trigger.Repetition.Duration -notin @($null, '', 'PT0S') -or
            $Trigger.Repetition.StopAtDurationEnd) {
            throw 'The existing SessionAware task must have only the dedicated user logon trigger.'
        }
        $TriggerSid = Resolve-TaskUserSid $Trigger.UserId
        if ($TriggerSid -ne $Sid) { throw 'The existing task logon trigger belongs to a different user.' }
    } elseif ($Action -eq 'Configure') {
        # Legacy Manual tasks remain readable/startable; Configure must not erase foreign recovery settings.
        $ExpectedLimit = if ($SavedIdle -eq 0) { 'PT0S' } else { 'PT2H' }
        if ($Triggers.Count -ne 0 -or $Settings.RestartCount -ne 0 -or
            $Settings.RestartInterval -notin @($null, '', 'PT0S') -or
            $Settings.MultipleInstances -ne 'IgnoreNew' -or $Settings.ExecutionTimeLimit -ne $ExpectedLimit) {
            throw 'Configure refused foreign trigger or task settings; no task was changed.'
        }
    }
}

function Assert-OwnedTask($Current) {
    $Sid = Resolve-TaskUserSid "$env:COMPUTERNAME\$WorkerUser"
    $PrincipalSid = Resolve-TaskUserSid $Current.Principal.UserId
    if (@($Current.Actions).Count -ne 1) { throw 'The existing task must have exactly one owned action.' }
    # rc6 omitted IdleTimeout and therefore used the session script's finite 1800-second default.
    $Match = [regex]::Match($Current.Actions[0].Arguments, '\A' + [regex]::Escape($BaseArguments) + ' -ReviewSeconds ([0-9]{1,2})(?: -IdleTimeout ([0-9]{1,4}))?(?: -RecoveryMode (Manual|SessionAware))?\z')
    if ($Sid -ne $PrincipalSid -or $Current.Principal.LogonType -ne 'Interactive' -or
        $Current.Principal.RunLevel -ne 'Limited' -or @($Current.Actions).Count -ne 1 -or
        $Current.Actions[0].Execute -ne $Execute -or -not $Match.Success -or
        $Current.Actions[0].WorkingDirectory -ne $Project) {
        throw 'Existing task does not match this user, project, root and worker configuration; it was not changed.'
    }
    $SavedReview = [int]$Match.Groups[1].Value
    $SavedIdle = if ($Match.Groups[2].Success) { [int]$Match.Groups[2].Value } else { 1800 }
    $SavedRecovery = if ($Match.Groups[3].Success) { $Match.Groups[3].Value } else { 'Manual' }
    if ($SavedReview -gt 60) { throw 'The existing task has an invalid review duration.' }
    if ($SavedIdle -ne 0 -and ($SavedIdle -lt 60 -or $SavedIdle -gt 7200)) { throw 'The existing task has an invalid idle timeout.' }
    Assert-RecoveryProfile $Current $SavedRecovery $SavedIdle $Sid
    if ($Action -in @('Register','Start') -and (
        ($ReviewWasRequested -and $SavedReview -ne $ReviewSeconds) -or
        ($IdleWasRequested -and $SavedIdle -ne $IdleTimeout) -or
        ($RecoveryWasRequested -and $SavedRecovery -ne $RecoveryMode))) {
        throw 'Requested configuration differs from the owned task. Stop it, then use Configure explicitly.'
    }
    $script:ConfiguredReview = $SavedReview
    $script:ConfiguredIdleTimeout = $SavedIdle
    $script:ConfiguredRecoveryMode = $SavedRecovery
}

function Get-Health {
    $Json = & $Runtime -m pbip_mcp.client --data-dir $Data status
    if ($LASTEXITCODE) { throw 'The MCP controller status call failed.' }
    return ($Json | ConvertFrom-Json)
}

function Test-FreshWorker($Worker, [DateTimeOffset]$NotBefore = [DateTimeOffset]::MinValue) {
    $Updated = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse([string]$Worker.updated_at, [ref]$Updated)) { return $false }
    $Now = [DateTimeOffset](Get-Date)
    # The controller's default heartbeat window is 15 seconds; also reject pre-launch/future records.
    return ($Worker.state -notin @('absent','stale','stopped') -and $Updated -ge $NotBefore -and
        $Updated -le $Now -and ($Now - $Updated).TotalSeconds -le 15)
}

function Test-WaitingWorker($Worker, [DateTimeOffset]$NotBefore = [DateTimeOffset]::MinValue) {
    return ($ConfiguredRecoveryMode -eq 'SessionAware' -and $Worker.ready -eq $false -and
        $Worker.state -eq 'waiting_for_session' -and
        ($Worker.session_id -is [int] -or $Worker.session_id -is [long]) -and $Worker.session_id -gt 0 -and
        (Test-FreshWorker $Worker $NotBefore))
}

function Test-ReadyWorker($Worker, [DateTimeOffset]$NotBefore = [DateTimeOffset]::MinValue) {
    return ($Worker.ready -eq $true -and $Worker.state -in @('idle','busy') -and
        (Test-FreshWorker $Worker $NotBefore))
}

function Get-OperationalStatus($Current, $Worker) {
    $StopRequested = Test-Path -LiteralPath $Stop -PathType Leaf
    $Running = $Current -and $Current.State -eq 'Running' -and -not $StopRequested
    return [ordered]@{
        task=$TaskName; registered=[bool]$Current
        state=$(if($Current){[string]$Current.State}else{'Absent'})
        review_seconds=$ConfiguredReview; idle_timeout=$ConfiguredIdleTimeout; recovery_mode=$ConfiguredRecoveryMode
        execution_time_limit=$(if($Current){[string]$Current.Settings.ExecutionTimeLimit}else{$null})
        restart_count=$(if($Current){[int]$Current.Settings.RestartCount}else{0})
        restart_interval=$(if($Current){[string]$Current.Settings.RestartInterval}else{$null})
        logon_user=$(if($Current -and $ConfiguredRecoveryMode -eq 'SessionAware'){$Current.Triggers[0].UserId}else{$null})
        stop_requested=[bool]$StopRequested
        ready=[bool]($Running -and (Test-ReadyWorker $Worker))
        waiting_for_session=[bool]($Running -and (Test-WaitingWorker $Worker))
        worker=$Worker
    }
}

if (-not (Test-Path $Runtime -PathType Leaf)) { throw 'Missing runtime; run setup.ps1 first.' }
if ($Task) { Assert-OwnedTask $Task }
if ($Action -eq 'Doctor') {
    & $Runtime (Join-Path $PSScriptRoot 'worker-doctor.py') --root $Root --desktop-exe $DesktopExe
    exit $LASTEXITCODE
}
$Reconfigure = $Action -eq 'Configure'
if ($Reconfigure) {
    if (-not $Task) { throw 'No owned task is registered. Use Register first.' }
    if ($Task.State -ne 'Ready') { throw 'Configure requires the owned task to be stopped in Ready state.' }
    $Health = Get-Health
    if ($null -eq $Health.queue.running -or $Health.queue.running -ne 0 -or $Health.worker.ready -or
        ($Health.worker.state -eq 'waiting_for_session' -and (Test-FreshWorker $Health.worker))) {
        throw 'Configure requires no running queue job and no ready or waiting worker; no task was changed.'
    }
    $Task = Get-ScheduledTask -TaskName $TaskName
    Assert-OwnedTask $Task
    if ($Task.State -ne 'Ready') { throw 'Task state changed during Configure; wait until the owned task is stopped in Ready state.' }
    if ($ReviewWasRequested) { $ConfiguredReview = $ReviewSeconds }
    if ($IdleWasRequested) { $ConfiguredIdleTimeout = $IdleTimeout }
    if ($RecoveryWasRequested) { $ConfiguredRecoveryMode = if ($RecoveryMode -eq 'SessionAware') { 'SessionAware' } else { 'Manual' } }
}
if (($Action -in @('Register','Start') -and -not $Task) -or $Reconfigure) {
    if ($ConfiguredRecoveryMode -eq 'SessionAware' -and $ConfiguredIdleTimeout -ne 0) {
        throw 'SessionAware recovery requires IdleTimeout 0; use Configure explicitly with both settings.'
    }
    if (-not (Test-Path $DesktopExe -PathType Leaf)) { throw 'Power BI Desktop is not installed.' }
    $Arguments = "$BaseArguments -ReviewSeconds $ConfiguredReview -IdleTimeout $ConfiguredIdleTimeout"
    if ($ConfiguredRecoveryMode -eq 'SessionAware') { $Arguments += ' -RecoveryMode SessionAware' }
    $Principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$WorkerUser" -LogonType Interactive -RunLevel Limited
    $TaskAction = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $Project
    $ExecutionLimit = if ($ConfiguredIdleTimeout -eq 0) { [TimeSpan]::Zero } else { New-TimeSpan -Hours 2 }
    $SettingsParameters = @{ ExecutionTimeLimit=$ExecutionLimit; MultipleInstances='IgnoreNew' }
    $Registration = @{}
    if ($ConfiguredRecoveryMode -eq 'SessionAware') {
        $SettingsParameters.RestartCount = 3
        $SettingsParameters.RestartInterval = New-TimeSpan -Minutes 1
        $Registration.Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\$WorkerUser"
    }
    $Settings = New-ScheduledTaskSettingsSet @SettingsParameters
    if ($Reconfigure) {
        # Preserve unrelated existing task settings rather than replacing them with cmdlet defaults.
        $SavedSettings = $Task.Settings
        foreach ($Name in @('ExecutionTimeLimit','MultipleInstances','RestartCount','RestartInterval')) {
            $SavedSettings.$Name = $Settings.$Name
        }
        $Settings = $SavedSettings
    }
    Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Principal $Principal -Settings $Settings @Registration -Force:$Reconfigure | Out-Null
    $Task = Get-ScheduledTask -TaskName $TaskName
    Assert-OwnedTask $Task
}
if ($Action -eq 'Start') {
    if ($Task.State -eq 'Disabled') { throw 'The owned task is Disabled; Start does not enable tasks.' }
    if ($Task.State -eq 'Running') {
        if (Test-Path -LiteralPath $Stop -PathType Leaf) { throw 'Graceful stop is pending; wait for the task to stop before Start. The stop marker was preserved.' }
        $Health = Get-Health
        $Task = Get-ScheduledTask -TaskName $TaskName
        Assert-OwnedTask $Task
        $Report = Get-OperationalStatus $Task $Health.worker
        if (-not $Report.ready -and -not $Report.waiting_for_session) { throw "Task is running but worker is not ready or safely waiting: $($Health.worker.message)" }
        $Report.already_running = $true
        $Report | ConvertTo-Json -Depth 5
        exit 0
    }
    if ($Task.State -ne 'Ready') { throw 'Start requires the owned task to be stopped in Ready state; a queued task or pending restart was not changed.' }
    $Health = Get-Health
    if ($Health.worker.ready -or
        ($Health.worker.state -eq 'waiting_for_session' -and (Test-FreshWorker $Health.worker))) {
        throw 'Another worker is ready or waiting for this queue; do not launch a duplicate.'
    }
    foreach ($Name in @('PBIPMCP-Demo','PBIPMCP-External')) {
        $Other = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if ($Other -and $Other.State -eq 'Running') { throw "Stop the existing $Name before starting the persistent worker." }
    }
    $Task = Get-ScheduledTask -TaskName $TaskName
    Assert-OwnedTask $Task
    if ($Task.State -ne 'Ready') { throw 'Task state changed before Start; any pending explicit stop was preserved. Wait until Ready.' }
    if (Test-Path -LiteralPath $Stop -PathType Leaf) { Remove-Item -LiteralPath $Stop }
    $Launch = [DateTimeOffset](Get-Date)
    Start-ScheduledTask -TaskName $TaskName
    $Until = (Get-Date).AddSeconds($WaitSeconds)
    do {
        Start-Sleep -Seconds 1
        $Health = Get-Health
        $Task = Get-ScheduledTask -TaskName $TaskName
        Assert-OwnedTask $Task
        $Accepted = ($Task.State -eq 'Running' -and -not (Test-Path -LiteralPath $Stop -PathType Leaf) -and
            ((Test-ReadyWorker $Health.worker $Launch) -or (Test-WaitingWorker $Health.worker $Launch)))
        if ($Accepted) { break }
    } while ((Get-Date) -lt $Until)
    if (-not $Accepted) { throw "Worker not ready or safely waiting: $($Health.worker.message). Reconnect/unlock its RDP desktop, inspect runs\worker-*, then Start again." }
    $Report = Get-OperationalStatus $Task $Health.worker
    if (-not $Report.ready -and -not $Report.waiting_for_session) { throw 'Readiness expired or an explicit stop arrived before Start completed; inspect Status before retrying.' }
    $Report.already_running = $false
    $Report | ConvertTo-Json -Depth 5
    exit 0
}
if ($Action -eq 'Stop') {
    if (-not $Task) {
        [ordered]@{task=$TaskName;registered=$false;state='Absent';already_stopped=$true;stop_requested=[bool](Test-Path -LiteralPath $Stop -PathType Leaf)} | ConvertTo-Json
        exit 0
    }
    [IO.Directory]::CreateDirectory($Data) | Out-Null
    [IO.File]::WriteAllText($Stop, (Get-Date).ToUniversalTime().ToString('o'))
    $Until = (Get-Date).AddSeconds($WaitSeconds)
    while ($Task.State -in @('Running','Queued') -and (Get-Date) -lt $Until) {
        Start-Sleep -Seconds 1
        $Task = Get-ScheduledTask -TaskName $TaskName
    }
    if ($Task.State -in @('Running','Queued')) { throw 'Graceful stop is pending until the owned conversion or queued task finishes; no process was killed. The stop marker remains active.' }
}
$Health = Get-Health
Get-OperationalStatus $Task $Health.worker | ConvertTo-Json -Depth 5
