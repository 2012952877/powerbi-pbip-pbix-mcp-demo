[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputZip,
    [string]$Manifest
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (Test-Path -LiteralPath $OutputZip) {
    throw 'The package already exists. Choose a new versioned filename; nothing was overwritten.'
}
$Runtime = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Arguments = @((Join-Path $PSScriptRoot 'build_package.py'), '--output', $OutputZip)
if ($Manifest) { $Arguments += @('--manifest', $Manifest) }
& $Runtime @Arguments
if ($LASTEXITCODE -ne 0) { throw 'Building the clean deployment package failed.' }
