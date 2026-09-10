[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Candidate,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ExpectedHash,
    [ValidatePattern('^[a-z0-9-]+$')][string]$ReleaseName = 'bidirectional-rc7'
)
$ErrorActionPreference = 'Stop'
$App = 'C:\PBIPMCP\app'
$Runtime = Join-Path $App '.venv\Scripts\python.exe'
$Release = Join-Path 'C:\PBIPMCP\releases' $ReleaseName
$Backup = Join-Path 'C:\PBIPMCP\backups' "app-before-$ReleaseName.zip"
$QueueBackup = Join-Path 'C:\PBIPMCP\backups' "queue-before-$ReleaseName.sqlite3"
if ((Get-ScheduledTask -TaskName 'PBIPMCP-Worker').State -ne 'Ready') {
    throw 'Stop the worker before installing a candidate.'
}
if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Stop every owned portal before installing a candidate.'
}
if ((Get-FileHash -LiteralPath $Candidate).Hash.ToLowerInvariant() -ne $ExpectedHash) {
    throw 'Candidate upload hash mismatch.'
}
foreach ($Path in @($Release, $Backup, $QueueBackup)) {
    if (Test-Path -LiteralPath $Path) { throw 'Never overwrite a release or backup.' }
}
Expand-Archive -LiteralPath $Candidate -DestinationPath $Release
if ((Get-FileHash -LiteralPath (Join-Path $Release 'requirements.lock')).Hash -ne
    (Get-FileHash -LiteralPath (Join-Path $App 'requirements.lock')).Hash) {
    throw 'Unexpected dependency change; do not install.'
}
$env:PBIP_QUEUE_BACKUP = $QueueBackup
@'
import os,sqlite3,time
from pathlib import Path
source=Path(r'C:\PBIPMCP\data\queue.sqlite3')
backup=Path(os.environ['PBIP_QUEUE_BACKUP'])
assert not backup.exists()
connection=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
try:
    assert connection.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]==0
    assert connection.execute("SELECT count(*) FROM downloads").fetchone()[0]==0
    assert connection.execute("SELECT count(*) FROM artifact_transfers WHERE deadline>?",(time.time(),)).fetchone()[0]==0
    destination=sqlite3.connect(backup)
    try:
        connection.backup(destination)
    finally:
        destination.close()
finally:
    connection.close()
print('Idle queue and downloads verified; consistent queue backup saved.')
'@ | & $Runtime -
if ($LASTEXITCODE -ne 0) { throw 'Idle queue preflight failed.' }
$Names = @('src','scripts','tests','deploy','pyproject.toml','requirements.lock','README.zh-CN.md')
$Previous = @($Names | ForEach-Object { Join-Path $App $_ } | Where-Object { Test-Path -LiteralPath $_ })
Compress-Archive -LiteralPath $Previous -DestinationPath $Backup
foreach ($Name in $Names) {
    Copy-Item -LiteralPath (Join-Path $Release $Name) -Destination $App -Recurse -Force
}
$SourceRoot = Join-Path $App 'src\pbip_mcp'
$Sources = @(Get-ChildItem -LiteralPath $SourceRoot -File -Recurse | Where-Object {
    $_.Extension -in @('.py','.html') -and $_.FullName -notmatch '\\__pycache__\\'
})
foreach ($Source in $Sources) {
    $Relative = $Source.FullName.Substring($SourceRoot.Length + 1)
    $Generated = Join-Path $App ('build\lib\pbip_mcp\' + $Relative)
    if (Test-Path -LiteralPath $Generated -PathType Leaf) { Remove-Item -LiteralPath $Generated }
}
& $Runtime -m pip install --quiet --no-deps $App
if ($LASTEXITCODE -ne 0) { throw 'Installation failed; do not start a mixed runtime.' }
foreach ($Source in $Sources) {
    $Relative = $Source.FullName.Substring($SourceRoot.Length + 1)
    $Installed = Join-Path $App ('.venv\Lib\site-packages\pbip_mcp\' + $Relative)
    $Released = Join-Path $Release ('src\pbip_mcp\' + $Relative)
    if ((Get-FileHash -LiteralPath $Source.FullName).Hash -ne (Get-FileHash -LiteralPath $Installed).Hash -or
        (Get-FileHash -LiteralPath $Source.FullName).Hash -ne (Get-FileHash -LiteralPath $Released).Hash) {
        throw "Installed/app/release source hash mismatch: $Relative"
    }
}
@'
from pathlib import Path
import pbip_mcp
from pbip_mcp.config import Config
from pbip_mcp.portal import create_portal
assert pbip_mcp.__version__=='0.2.4'
assert Path(pbip_mcp.__file__).resolve()==Path(r'C:\PBIPMCP\app\.venv\Lib\site-packages\pbip_mcp\__init__.py')
create_portal(Config(Path(r'C:\PBIPMCP\data')),Path(r'C:\PBIPMCP\pilot-auth-bidir\users.json'),
    origin='https://pbip-mcp-bidir-2d79425f.azurewebsites.net',allow_public_origin=True)
print('Installed 0.2.4 public-origin construction verified without starting a listener or Desktop.')
'@ | & $Runtime -
if ($LASTEXITCODE -ne 0) { throw 'Installed runtime validation failed.' }
$Receipt = [ordered]@{
    deployed_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    package = (Split-Path -Leaf $Candidate)
    sha256 = $ExpectedHash
    source_installed_release_files_matched = $Sources.Count
    version = '0.2.4'
    backup = $Backup
    backup_sha256 = (Get-FileHash -LiteralPath $Backup).Hash.ToLowerInvariant()
    queue_backup = $QueueBackup
    worker_started = $false
    portal_stopped_during_upgrade = $true
    existing_job_results_and_samples_untouched = $true
}
$Json = $Receipt | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $Release 'deployment-receipt.json'), $Json)
$Json
