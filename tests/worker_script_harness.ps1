param([string]$Project, [string]$Root)
$ErrorActionPreference = 'Stop'
$Case = $env:PBIP_WORKER_SCRIPT_UNIT_CASE | ConvertFrom-Json
$Runtime = Join-Path $Project '.venv\Scripts\python.exe'
$Desktop = Join-Path $Root 'unit-never-launched.exe'
$StopFile = Join-Path $Root 'data\worker.stop'
$SessionScript = Join-Path $Project 'scripts\run-worker-session.ps1'
$Manager = Join-Path $Project 'scripts\manage-worker.ps1'
$Execute = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Base = "-NoProfile -ExecutionPolicy Bypass -File `"$SessionScript`" -Root `"$Root`" -DesktopExe `"$Desktop`""
$SavedArguments = "$Base -ReviewSeconds $($Case.saved_review)"
if ($null -ne $Case.saved_idle) { $SavedArguments += " -IdleTimeout $($Case.saved_idle)" }
$SavedArguments += $Case.extra_arguments
$Unit = @{
    task = $null; registrations = 0; starts = 0; clears = 0; health_calls = 0; forced = $false
    ready = [bool]$Case.ready; running = $Case.running; remote = $null
}
if ($Case.present) {
    $Unit.task = [pscustomobject]@{
        State = $Case.state
        Principal = [pscustomobject]@{
            UserId = $(if ($Case.foreign) { 'UNOWNED\someone' } else { "$env:COMPUTERNAME\unitworker" })
            LogonType = 'Interactive'; RunLevel = 'Limited'
        }
        Actions = @([pscustomobject]@{ Execute = $Execute; Arguments = $SavedArguments; WorkingDirectory = $Project })
        Settings = [pscustomobject]@{ ExecutionTimeLimit = $(if ($Case.saved_idle -eq 0) { 'PT0S' } else { 'PT2H' }) }
    }
}

# All host/task/network boundaries are intercepted; the production scripts themselves execute unchanged.
function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    if ($TaskName -eq 'PBIPMCP-Worker') { return $Unit.task }
    if ($TaskName -notin @('PBIPMCP-Demo', 'PBIPMCP-External')) { throw 'Unexpected task query.' }
}
function New-Object {
    param($TypeName, $ArgumentList)
    if ($TypeName -ne 'Security.Principal.NTAccount') { throw 'Unexpected native object.' }
    $Object = [pscustomobject]@{ UnitId = ($ArgumentList -join '\') }
    $Object | Add-Member ScriptMethod Translate {
        param($TargetType)
        [pscustomobject]@{ Value = $this.UnitId }
    }
    return $Object
}
function Test-Path {
    param($Path, $LiteralPath, $PathType)
    $Target = if ($LiteralPath) { $LiteralPath } else { $Path }
    if ($Target -in @($Runtime, $Desktop, (Join-Path $Root 'unit-identity'), (Join-Path $Root 'unit-hosts'))) { return $true }
    if ($Target -eq $StopFile) { return [IO.File]::Exists($Target) }
    throw "Unexpected filesystem query: $Target"
}
function Invoke-UnitRuntime {
    if (($args -join ' ') -ne "-m pbip_mcp.client --data-dir $(Join-Path $Root 'data') status") {
        throw 'Unexpected runtime invocation; no interpreter was launched.'
    }
    $Unit.health_calls++
    $global:LASTEXITCODE = 0
    @{ worker = @{ ready = $Unit.ready; message = 'UNIT boundary only' }; queue = @{ running = $Unit.running } } |
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
    param([TimeSpan]$ExecutionTimeLimit, $MultipleInstances)
    if ($MultipleInstances -ne 'IgnoreNew') { throw 'Single-instance protection was lost.' }
    [pscustomobject]@{ ExecutionTimeLimit = [Xml.XmlConvert]::ToString($ExecutionTimeLimit) }
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Principal, $Settings, [switch]$Force)
    if ($TaskName -ne 'PBIPMCP-Worker') { throw 'Unexpected task registration.' }
    if ($Unit.task -and -not $Force) { throw 'Replacing a task requires an explicit force.' }
    $Unit.registrations++
    $Unit.forced = [bool]$Force
    $Unit.task = [pscustomobject]@{ State = 'Ready'; Actions = @($Action); Principal = $Principal; Settings = $Settings }
    return $Unit.task
}
function Start-ScheduledTask {
    param($TaskName)
    if ($TaskName -ne 'PBIPMCP-Worker') { throw 'Unexpected task start.' }
    $Unit.starts++
    $Unit.task.State = 'Running'
    $Unit.ready = $true
}
function Start-Sleep {
    param($Seconds)
    if ([IO.File]::Exists($StopFile)) {
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
    $global:LASTEXITCODE = 0
}
Set-Alias -Name ssh -Value Invoke-UnitSSH
$Parameters = @{}
foreach ($Property in $Case.parameters.PSObject.Properties) { $Parameters[$Property.Name] = $Property.Value }
$Result = [ordered]@{ ok = $false; error = $null; output = $null }
try {
    foreach ($Name in @('manage-worker.ps1', 'run-worker-session.ps1', 'remote-worker.ps1')) {
        $Tokens = $null
        $ParseErrors = $null
        $Ast = [Management.Automation.Language.Parser]::ParseFile(
            (Join-Path $Project "scripts\$Name"), [ref]$Tokens, [ref]$ParseErrors)
        if ($ParseErrors.Count) { throw ($ParseErrors.Message -join '; ') }
        if ($Case.mode -eq 'parameters' -and $Case.script -eq $Name) {
            $Probe = [scriptblock]::Create($Ast.ParamBlock.Extent.Text + "`n[ordered]@{idle_timeout=`$IdleTimeout;review_seconds=`$ReviewSeconds} | ConvertTo-Json -Compress")
        }
    }
    if ($Case.mode -eq 'parameters') {
        $Result.output = (& $Probe @Parameters) | ConvertFrom-Json
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
$Result.remote = $Unit.remote
$Result | ConvertTo-Json -Depth 8 -Compress
