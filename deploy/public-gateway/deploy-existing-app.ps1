[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Subscription,
    [Parameter(Mandatory=$true)][string]$ResourceGroup,
    [Parameter(Mandatory=$true)][string]$AppName,
    [Parameter(Mandatory=$true)][string]$GatewayZip,
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$BackendHost,
    [int]$BackendPort = 8765
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $GatewayZip -PathType Leaf)) { throw 'Gateway ZIP does not exist.' }
$Origin = "https://$AppName.azurewebsites.net"
az webapp update --subscription $Subscription --resource-group $ResourceGroup --name $AppName --https-only true --output none
if ($LASTEXITCODE -ne 0) { throw 'HTTPS-only configuration failed.' }
az webapp config set --subscription $Subscription --resource-group $ResourceGroup --name $AppName `
    --startup-file 'npm start' --always-on true --min-tls-version 1.2 --ftps-state Disabled --output none
if ($LASTEXITCODE -ne 0) { throw 'App startup configuration failed.' }
az webapp config appsettings set --subscription $Subscription --resource-group $ResourceGroup --name $AppName `
    --settings "PUBLIC_ORIGIN=$Origin" "BACKEND_HOST=$BackendHost" "BACKEND_PORT=$BackendPort" --output none
if ($LASTEXITCODE -ne 0) { throw 'Non-secret gateway configuration failed.' }
az webapp config set --subscription $Subscription --resource-group $ResourceGroup --name $AppName `
    --generic-configurations '{"healthCheckPath":"/healthz"}' --output none
if ($LASTEXITCODE -ne 0) { throw 'Backend-aware health path configuration failed.' }
az webapp deploy --subscription $Subscription --resource-group $ResourceGroup --name $AppName `
    --src-path $GatewayZip --type zip --clean true --restart true --output none
if ($LASTEXITCODE -ne 0) { throw 'Gateway ZIP deployment failed.' }
Write-Output $Origin
