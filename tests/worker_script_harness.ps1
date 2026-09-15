param([string]$Project, [string]$Root)
$ErrorActionPreference = 'Stop'
$Cases = $env:PBIP_WORKER_SCRIPT_UNIT_CASE | ConvertFrom-Json
$ScriptAsts = @{}
foreach ($Name in @('manage-worker.ps1', 'run-worker-session.ps1', 'remote-worker.ps1')) {
    $Tokens = $null
    $ParseErrors = $null
    $ScriptAsts[$Name] = [Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $Project "scripts\$Name"), [ref]$Tokens, [ref]$ParseErrors)
    if ($ParseErrors.Count) { throw ($ParseErrors.Message -join '; ') }
}

function Invoke-UnitCase($Case, $Root) {
$Runtime = Join-Path $Project '.venv\Scripts\python.exe'
$Desktop = Join-Path $Root 'unit-never-launched.exe'
$StopFile = Join-Path $Root 'data\worker.stop'
$SessionScript = Join-Path $Project 'scripts\run-worker-session.ps1'
$Manager = Join-Path $Project 'scripts\manage-worker.ps1'
$Execute = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Base = "-NoProfile -ExecutionPolicy Bypass -File `"$SessionScript`" -Root `"$Root`" -DesktopExe `"$Desktop`""
$SavedArguments = "$Base -ReviewSeconds $($Case.saved_review)"
if ($null -ne $Case.saved_idle) { $SavedArguments += " -IdleTimeout $($Case.saved_idle)" }
if ($Case.saved_recovery) { $SavedArguments += " -RecoveryMode $($Case.saved_recovery)" }
$SavedArguments += $Case.extra_arguments
$Unit = @{
    task = $null; registrations = 0; starts = 0; clears = 0; health_calls = 0; forced = $false
    ready = [bool]$Case.ready; running = $Case.running; remote = $null
    now = [DateTime]::UtcNow; task_queries = 0; sleeps = 0; launched = $null
    python_calls = 0; python_arguments = @(); run_creations = 0; doctor_calls = 0
    runtime_preference = $null; remote_arguments = @(); preflight_calls = 0
    post_start_stop_queries = 0
}
function New-UnitLogonTrigger($User) {
    return [pscustomobject]@{
        CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskLogonTrigger' }
        UserId = $User; Enabled = $true; StartBoundary = ''; EndBoundary = ''; Delay = ''
        ExecutionTimeLimit = ''; Repetition = [pscustomobject]@{ Interval = ''; Duration = ''; StopAtDurationEnd = $false }
    }
}
if ($Case.present) {
    $SessionAware = $Case.saved_recovery -eq 'SessionAware'
    $Triggers = @()
    if ($Case.native_trigger_shape) { $Triggers = $null }
    if ($SessionAware -or $Case.foreign_trigger) { $Triggers = @(New-UnitLogonTrigger "$env:COMPUTERNAME\unitworker") }
    foreach ($Property in $Case.trigger_changes.PSObject.Properties) { $Triggers[0].$($Property.Name) = $Property.Value }
    if ($Case.trigger_type) { $Triggers[0].CimClass.CimClassName = $Case.trigger_type }
    if ($Case.extra_trigger) { $Triggers += New-UnitLogonTrigger "$env:COMPUTERNAME\other" }
    if ($Case.null_triggers) { $Triggers = $null }
    if ($Case.empty_triggers) { $Triggers = @() }
    $Settings = [pscustomobject]@{
        ExecutionTimeLimit = $(if ($Case.saved_idle -eq 0) { 'PT0S' } else { 'PT2H' })
        MultipleInstances = 'IgnoreNew'; RestartCount = $(if ($SessionAware) { 3 } else { 0 })
        RestartInterval = $(if ($SessionAware) { 'PT1M' } else { '' })
        DisallowStartIfOnBatteries = $false
    }
    foreach ($Property in $Case.settings.PSObject.Properties) { $Settings.$($Property.Name) = $Property.Value }
    $Unit.task = [pscustomobject]@{
        State = $Case.state
        Principal = [pscustomobject]@{
            UserId = $(if ($Case.foreign) { 'UNOWNED\someone' } elseif ($Case.principal_user_id) { $Case.principal_user_id } else { "$env:COMPUTERNAME\unitworker" })
            LogonType = $(if ($Case.logon_type) { $Case.logon_type } else { 'Interactive' })
            RunLevel = $(if ($Case.run_level) { $Case.run_level } else { 'Limited' })
        }
        Actions = @([pscustomobject]@{ Execute = $Execute; Arguments = $SavedArguments; WorkingDirectory = $Project })
        Settings = $Settings; Triggers = $Triggers
    }
    if ($Case.extra_action) { $Unit.task.Actions += $Unit.task.Actions[0] }
    if ($Case.foreign_cwd) { $Unit.task.Actions[0].WorkingDirectory = $Root }
    if ($Case.foreign_execute) { $Unit.task.Actions[0].Execute = 'foreign.exe' }
}

# Manager/remote scripts execute unchanged. Session tests substitute ONLY the two static preflight probes.
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    if ($TaskName -eq 'PBIPMCP-Worker') {
        $Unit.task_queries++
        if ($Unit.task_queries -eq 2 -and $Case.state_on_second_query) { $Unit.task.State = $Case.state_on_second_query }
        return $Unit.task
    }
    if ($TaskName -notin @('PBIPMCP-Demo', 'PBIPMCP-External')) { throw 'Unexpected task query.' }
}
function New-Object {
    param($TypeName, $ArgumentList)
    if ($TypeName -ne 'Security.Principal.NTAccount') { throw 'Unexpected native object.' }
    $Object = [pscustomobject]@{ UnitId = ($ArgumentList -join '\') }
    $Object | Add-Member ScriptMethod Translate {
        param($TargetType)
        [pscustomobject]@{ Value = $(if ($this.UnitId -eq "$env:COMPUTERNAME\unitworker") { 'S-1-5-21-100-200-300-1001' } else { 'S-1-5-21-100-200-300-1002' }) }
    }
    return $Object
}
function Test-Path {
    param($Path, $LiteralPath, $PathType)
    $Target = if ($LiteralPath) { $LiteralPath } else { $Path }
    if ($Target -in @($Runtime, $Desktop, (Join-Path $Root 'unit-identity'), (Join-Path $Root 'unit-hosts'))) { return $true }
    if ($Target -eq $StopFile) {
        if ($Unit.starts) {
            $Unit.post_start_stop_queries++
            if ($Case.expire_before_report -and $Unit.post_start_stop_queries -eq 2) { $Unit.now = $Unit.now.AddSeconds(16) }
        }
        return [IO.File]::Exists($Target)
    }
    throw "Unexpected filesystem query: $Target"
}
function Invoke-UnitRuntime {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq (Join-Path $Project 'scripts\worker-doctor.py')) {
        $Unit.doctor_calls++
        return
    }
    if ($Case.mode -eq 'session') {
        if ($args[0] -ne '-m' -or $args[1] -ne 'pbip_mcp.worker') { throw 'Unexpected session runtime invocation.' }
        $Unit.python_calls++
        $Unit.python_arguments = @($args)
        $Unit.runtime_preference = [string]$ErrorActionPreference
        $global:LASTEXITCODE = [int]$Case.worker_exit_code
        Write-Output 'UNIT runtime boundary only'
        return
    }
    if (($args -join ' ') -ne "-m pbip_mcp.client --data-dir $(Join-Path $Root 'data') status") {
        throw 'Unexpected runtime invocation; no interpreter was launched.'
    }
    $Unit.health_calls++
    $State = if ($Case.worker_state) { $Case.worker_state } elseif ($Unit.ready) { 'idle' } else { 'stopped' }
    if ($Unit.starts -and $Case.after_start_worker_state) { $State = $Case.after_start_worker_state }
    $Updated = $Unit.now.AddSeconds(-[double]$Case.heartbeat_age).ToString('o')
    if ($Unit.starts -and $Case.old_heartbeat_after_start) { $Updated = $Unit.launched.AddSeconds(-1).ToString('o') }
    if ($Case.PSObject.Properties['updated_at']) { $Updated = $Case.updated_at }
    $SessionId = if ($Case.PSObject.Properties['session_id']) { $Case.session_id } else { 2 }
    @{ worker = @{ ready = $Unit.ready; state = $State; message = 'UNIT boundary only'; updated_at = $Updated; session_id = $SessionId }; queue = @{ running = $Unit.running } } |
        ConvertTo-Json -Depth 5 -Compress
}
Set-Alias -Name $Runtime -Value Invoke-UnitRuntime
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
}
function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
}
function New-ScheduledTaskSettingsSet {
    param([TimeSpan]$ExecutionTimeLimit, $MultipleInstances, [int]$RestartCount = 0, [TimeSpan]$RestartInterval = [TimeSpan]::Zero)
    if ($MultipleInstances -ne 'IgnoreNew') { throw 'Single-instance protection was lost.' }
    [pscustomobject]@{
        ExecutionTimeLimit = [Xml.XmlConvert]::ToString($ExecutionTimeLimit); MultipleInstances = $MultipleInstances
        RestartCount = $RestartCount; RestartInterval = [Xml.XmlConvert]::ToString($RestartInterval)
        DisallowStartIfOnBatteries = $false
    }
}
function New-ScheduledTaskTrigger {
    param([switch]$AtLogOn, $User)
    if (-not $AtLogOn -or $User -ne "$env:COMPUTERNAME\unitworker") { throw 'Only the exact dedicated user logon trigger is allowed.' }
    New-UnitLogonTrigger $User
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Principal, $Settings, $Trigger = @(), [switch]$Force)
    if ($TaskName -ne 'PBIPMCP-Worker') { throw 'Unexpected task registration.' }
    if ($Unit.task -and -not $Force) { throw 'Replacing a task requires an explicit force.' }
    $Unit.registrations++
    $Unit.forced = [bool]$Force
    $Unit.task = [pscustomobject]@{ State = 'Ready'; Actions = @($Action); Principal = $Principal; Settings = $Settings; Triggers = @($Trigger) }
    if ($Case.native_trigger_shape -and @($Trigger).Count -eq 0) { $Unit.task.Triggers = $null }
    return $Unit.task
}
function Start-ScheduledTask {
    param($TaskName)
    if ($TaskName -ne 'PBIPMCP-Worker') { throw 'Unexpected task start.' }
    $Unit.starts++
    $Unit.launched = $Unit.now
    $Unit.task.State = if ($Case.after_start_task_state) { $Case.after_start_task_state } else { 'Running' }
    $Unit.ready = if ($Case.PSObject.Properties['after_start_ready']) { [bool]$Case.after_start_ready } else { $true }
}
function Get-Date { return $Unit.now }
function New-Item {
    param($ItemType, $Path, $ErrorAction)
    if ($ItemType -ne 'Directory' -or -not $Path.StartsWith((Join-Path $Root 'runs\worker-'))) { throw 'Unexpected directory creation.' }
    $Unit.run_creations++
    [IO.Directory]::CreateDirectory($Path)
}
function Start-Sleep {
    param($Seconds)
    $Unit.sleeps++
    $Unit.now = $Unit.now.AddSeconds($Seconds)
    if ($Case.stop_during_start -and $Unit.starts) { [IO.File]::WriteAllText($StopFile, 'UNIT concurrent explicit stop') }
    if ([IO.File]::Exists($StopFile) -and -not $Case.stop_pending) {
        $Unit.task.State = 'Ready'
        $Unit.ready = $false
    }
}
function Remove-Item {
    param($LiteralPath)
    if ($LiteralPath -ne $StopFile) { throw 'Unexpected removal.' }
    [IO.File]::Delete($LiteralPath)
    $Unit.clears++
}
function Invoke-UnitSSH {
    $Encoded = $args[-1]
    if ($args[-2] -ne '-EncodedCommand') { throw 'Unexpected SSH shape; no connection was made.' }
    $Unit.remote = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Encoded))
    $Unit.remote_arguments = @($args)
    $global:LASTEXITCODE = 0
}
Set-Alias -Name ssh -Value Invoke-UnitSSH
function Get-UnitSessionId {
    $Unit.preflight_calls++
    if ($Case.PSObject.Properties['preflight_session_id']) { return $Case.preflight_session_id }
    return 2
}
function Get-UnitUserSid {
    $Unit.preflight_calls++
    if ($Case.preflight_sid) { return $Case.preflight_sid }
    return 'S-1-5-21-UNIT-NORMAL-USER'
}
$Parameters = @{}
foreach ($Property in $Case.parameters.PSObject.Properties) { $Parameters[$Property.Name] = $Property.Value }
$Result = [ordered]@{ ok = $false; error = $null; output = $null }
try {
    if ($Case.mode -eq 'parameters') {
        $Ast = $ScriptAsts[$Case.script]
        $Probe = [scriptblock]::Create($Ast.ParamBlock.Extent.Text + "`n[ordered]@{idle_timeout=`$IdleTimeout;review_seconds=`$ReviewSeconds;recovery_mode=`$RecoveryMode} | ConvertTo-Json -Compress")
        $Result.output = (& $Probe @Parameters) | ConvertFrom-Json
    } elseif ($Case.mode -eq 'session') {
        $Ast = $ScriptAsts['run-worker-session.ps1']
        $Substitutions = @{
            '[Diagnostics.Process]::GetCurrentProcess().SessionId' = '(Get-UnitSessionId)'
            '[Security.Principal.WindowsIdentity]::GetCurrent().User.Value' = '(Get-UnitUserSid)'
        }
        $Nodes = @($Ast.FindAll({
            param($Node)
            $Node -is [Management.Automation.Language.MemberExpressionAst] -and $Substitutions.ContainsKey($Node.Extent.Text)
        }, $true))
        if ($Nodes.Count -ne 2) { throw 'Session preflight changed; review the boundary interception before testing.' }
        $Source = $Ast.Extent.Text
        foreach ($Node in ($Nodes | Sort-Object { $_.Extent.StartOffset } -Descending)) {
            $Source = $Source.Remove($Node.Extent.StartOffset, $Node.Extent.EndOffset - $Node.Extent.StartOffset).Insert(
                $Node.Extent.StartOffset, $Substitutions[$Node.Extent.Text])
        }
        $ProbeFile = Join-Path $Root 'scripts\run-worker-session.ps1'
        [IO.Directory]::CreateDirectory((Split-Path -Parent $ProbeFile)) | Out-Null
        [IO.File]::WriteAllText($ProbeFile, $Source)
        Set-Alias -Name (Join-Path $Root '.venv\Scripts\python.exe') -Value Invoke-UnitRuntime
        $Parameters.Root = $Root
        $Parameters.DesktopExe = $Desktop
        $Result.output = (& $ProbeFile @Parameters) -join "`n"
        $Result.exit_code = $LASTEXITCODE
    } elseif ($Case.mode -eq 'remote') {
        $Parameters.Identity = Join-Path $Root 'unit-identity'
        $Parameters.KnownHosts = Join-Path $Root 'unit-hosts'
        $Parameters.HostKeyAlias = 'unit-only'
        & (Join-Path $Project 'scripts\remote-worker.ps1') @Parameters | Out-Null
    } else {
        $Parameters.Root = $Root
        $Parameters.WorkerUser = 'unitworker'
        $Parameters.DesktopExe = $Desktop
        $Result.output = (& $Manager @Parameters) | ConvertFrom-Json
    }
    $Result.ok = $true
} catch {
    $Result.error = $_.Exception.Message
}
$Result.registrations = $Unit.registrations
$Result.forced = $Unit.forced
$Result.starts = $Unit.starts
$Result.clears = $Unit.clears
$Result.health_calls = $Unit.health_calls
$Result.stop_exists = [IO.File]::Exists($StopFile)
$Result.arguments = if ($Unit.task) { $Unit.task.Actions[0].Arguments } else { $null }
$Result.execution_limit = if ($Unit.task) { $Unit.task.Settings.ExecutionTimeLimit } else { $null }
$Result.settings = if ($Unit.task) { $Unit.task.Settings } else { $null }
$Result.principal = if ($Unit.task) { $Unit.task.Principal } else { $null }
$Result.triggers = @()
if ($Unit.task) { $Result.triggers = @($Unit.task.Triggers) }
$Result.remote = $Unit.remote
$Result.remote_arguments = $Unit.remote_arguments
$Result.sleeps = $Unit.sleeps
$Result.python_calls = $Unit.python_calls
$Result.python_arguments = $Unit.python_arguments
$Result.run_creations = $Unit.run_creations
$Result.runtime_preference = $Unit.runtime_preference
$Result.doctor_calls = $Unit.doctor_calls
$Result.preflight_calls = $Unit.preflight_calls
return $Result
}

$Index = 0
$Results = @(
    foreach ($Case in $Cases) {
        Invoke-UnitCase $Case (Join-Path $Root "case-$Index")
        $Index++
    }
)
ConvertTo-Json -InputObject $Results -Depth 10 -Compress
