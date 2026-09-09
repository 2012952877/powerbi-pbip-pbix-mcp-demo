[CmdletBinding()]
param(
    [ValidateSet('Apply', 'Status', 'Remove')]
    [string]$Action = 'Status',
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^(?:\d{1,3}\.){3}\d{1,3}$')][string]$BindAddress,
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^(?:\d{1,3}\.){3}\d{1,3}/26$')][string]$GatewaySubnet
)
$ErrorActionPreference = 'Stop'
$Name = 'PBIPMCP-PublicGateway-8765'
$AllowedAddresses = @($GatewaySubnet, ($GatewaySubnet -replace '/26$', '/255.255.255.192'))
$Existing = Get-NetFirewallRule -Name $Name -ErrorAction SilentlyContinue
if ($Existing) {
    $Address = $Existing | Get-NetFirewallAddressFilter
    $Port = $Existing | Get-NetFirewallPortFilter
    if ($Existing.Direction -ne 'Inbound' -or $Existing.Action -ne 'Allow' -or
        $Address.LocalAddress -ne $BindAddress -or $Address.RemoteAddress -notin $AllowedAddresses -or
        $Port.LocalPort -ne '8765' -or $Port.Protocol -ne 'TCP') {
        throw 'Existing named firewall rule differs from the approved scope; it was not changed.'
    }
}
if ($Action -eq 'Apply' -and -not $Existing) {
    New-NetFirewallRule -Name $Name -DisplayName $Name -Direction Inbound -Action Allow `
        -Protocol TCP -LocalAddress $BindAddress -LocalPort 8765 -RemoteAddress $GatewaySubnet `
        -Profile Any -EdgeTraversalPolicy Block | Out-Null
}
if ($Action -eq 'Remove' -and $Existing) {
    Remove-NetFirewallRule -Name $Name
}
[ordered]@{
    name = $Name
    exists = [bool](Get-NetFirewallRule -Name $Name -ErrorAction SilentlyContinue)
    local_address = $BindAddress
    local_port = 8765
    allowed_source = $GatewaySubnet
    azure_nsg_modified = $false
} | ConvertTo-Json -Compress
