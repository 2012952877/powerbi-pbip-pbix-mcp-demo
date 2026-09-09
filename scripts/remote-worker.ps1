[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('Register','Start','Stop','Status','Doctor')][string]$Action,
    [Parameter(Mandatory=$true)][string]$Identity,
    [Parameter(Mandatory=$true)][string]$KnownHosts,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9.:-]+$')][string]$HostKeyAlias,
    [ValidateRange(1,65535)][int]$Port = 50022,
    [ValidatePattern('^[A-Za-z0-9_.-]+@(127\.0\.0\.1|localhost)$')][string]$Target = 'pbipdemo@127.0.0.1',
    [ValidateRange(0,60)][int]$ReviewSeconds = 0,
    [ValidateRange(1,700)][int]$WaitSeconds = 30
)
$ErrorActionPreference='Stop'
if (-not (Test-Path -LiteralPath $Identity -PathType Leaf) -or -not (Test-Path -LiteralPath $KnownHosts -PathType Leaf)) {
    throw 'Provide an existing approved SSH identity and pinned known_hosts file.'
}
$ReviewArgument = ''
if ($PSBoundParameters.ContainsKey('ReviewSeconds')) { $ReviewArgument = " -ReviewSeconds $ReviewSeconds" }
$Remote = "`$ErrorActionPreference='Stop'; `$ProgressPreference='SilentlyContinue'; & 'C:\PBIPMCP\app\scripts\manage-worker.ps1' -Action $Action$ReviewArgument -WaitSeconds $WaitSeconds"
$Encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Remote))
$Options = @(
    '-T', '-p', [string]$Port, '-i', [IO.Path]::GetFullPath($Identity),
    '-o', ('UserKnownHostsFile=' + [IO.Path]::GetFullPath($KnownHosts)),
    '-o', "HostKeyAlias=$HostKeyAlias", '-o', 'StrictHostKeyChecking=yes',
    '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=45',
    '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3'
)
& ssh @Options $Target powershell.exe -NoProfile -NonInteractive -EncodedCommand $Encoded
exit $LASTEXITCODE
