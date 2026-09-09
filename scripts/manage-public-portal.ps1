[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Register','Start','Stop','Status')][string]$Action,
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^https://[a-z0-9-]+\.azurewebsites\.net$')][string]$PublicOrigin,
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^(?:\d{1,3}\.){3}\d{1,3}$')][string]$BindAddress,
    [ValidateRange(1,120)][int]$WaitSeconds = 30
)
$ErrorActionPreference = 'Stop'
$Name = 'PBIPMCP-PublicPortal'
$User = "$env:COMPUTERNAME\pbipdemo"
$App = Split-Path -Parent $PSScriptRoot
$Execute = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Script = Join-Path $PSScriptRoot 'run-public-portal-session.ps1'
$Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Script`" -PublicOrigin `"$PublicOrigin`" -BindAddress `"$BindAddress`""
$PidFile = 'C:\PBIPMCP\pilot-auth-bidir\public-portal.pid'
$HostName = ([uri]$PublicOrigin).Host
$Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue

function Assert-OwnedTask($Current) {
    $ExpectedSid = (New-Object Security.Principal.NTAccount($User)).Translate([Security.Principal.SecurityIdentifier]).Value
    $ActualSid = (New-Object Security.Principal.NTAccount($Current.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($ActualSid -ne $ExpectedSid -or $Current.Principal.LogonType -ne 'Interactive' -or
        $Current.Principal.RunLevel -ne 'Limited' -or @($Current.Actions).Count -ne 1 -or
        $Current.Actions[0].Execute -ne $Execute -or $Current.Actions[0].Arguments -ne $Arguments -or
        $Current.Actions[0].WorkingDirectory -ne $App) {
        throw 'Existing portal task has different ownership or action; it was not changed.'
    }
}

function Get-PortalHealth {
    Add-Type -AssemblyName System.Net.Http
    $Handler = New-Object System.Net.Http.HttpClientHandler
    $Handler.UseProxy = $false
    $Client = New-Object System.Net.Http.HttpClient($Handler)
    $Client.Timeout = [TimeSpan]::FromSeconds(5)
    try {
        $Request = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, "http://${BindAddress}:8765/api/session")
        $Request.Headers.Host = $HostName
        try {
            $Response = $Client.SendAsync($Request).GetAwaiter().GetResult()
            try {
                return [int]$Response.StatusCode -eq 401
            } finally { $Response.Dispose() }
        } finally { $Request.Dispose() }
    } catch [System.Net.Http.HttpRequestException] {
        return $false
    } catch [System.Threading.Tasks.TaskCanceledException] {
        return $false
    } finally {
        $Client.Dispose()
        $Handler.Dispose()
    }
}

if ($Task) { Assert-OwnedTask $Task }
if ($Action -eq 'Register' -and -not $Task) {
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $TaskAction = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $App
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName $Name -Action $TaskAction -Trigger $Trigger `
        -Principal $Principal -Settings $Settings | Out-Null
    $Task = Get-ScheduledTask -TaskName $Name
}
if ($Action -in @('Start','Stop') -and -not $Task) { throw 'Register this owned portal task first.' }
if ($Action -eq 'Start') {
    if ($Task.State -eq 'Disabled') { throw 'The portal task is explicitly disabled; it was not enabled.' }
    if ($Task.State -ne 'Running') {
        if (Get-NetTCPConnection -LocalAddress $BindAddress -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
            throw 'Another private portal listener already exists; stop the owned predecessor first.'
        }
        if (Test-Path -LiteralPath $PidFile) {
            & (Join-Path $PSScriptRoot 'stop-public-portal.ps1') -PidFile $PidFile | Out-Null
        }
        Start-ScheduledTask -TaskName $Name
    }
    $Until = (Get-Date).AddSeconds($WaitSeconds)
    do {
        $Healthy = Get-PortalHealth
        if ($Healthy) { break }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $Until)
    if (-not $Healthy) { throw 'Portal task did not become healthy; confirm pbipdemo is logged in and inspect its private run logs.' }
}
if ($Action -eq 'Stop') {
    # Disable first so an explicit stop cannot trigger a failure restart or next-logon restart.
    Disable-ScheduledTask -TaskName $Name | Out-Null
    if ($Task.State -eq 'Running') { Stop-ScheduledTask -TaskName $Name }
    & (Join-Path $PSScriptRoot 'stop-public-portal.ps1') -PidFile $PidFile | Out-Null
}
$Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
[ordered]@{
    task = $Name
    registered = [bool]$Task
    task_state = $(if ($Task) { [string]$Task.State } else { 'Absent' })
    portal_healthy = $(if ($Action -eq 'Register') { $false } else { Get-PortalHealth })
    origin = $PublicOrigin
    logon_type = 'Interactive'
    run_level = 'Limited'
    requires_pbipdemo_logon = $true
    requires_local_ssh_session = $false
    worker_started_or_stopped = $false
    explicit_stop_disables_task = $true
} | ConvertTo-Json -Compress
