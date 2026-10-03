<#
.SYNOPSIS
    Configure a static IP on a Windows Ethernet or Wi-Fi adapter for the
    Distributed Task Offloading System.

.DESCRIPTION
    A direct cable between two NICs does not hand out DHCP addresses, so both
    machines have to be given static addresses on the same private subnet. This
    script does that in one command and then verifies the result.

    Run it on BOTH machines with the matching -Role value:

        # on the worker (the machine with the GPU)
        powershell -ExecutionPolicy Bypass -File tools\configure_network.ps1 `
            -Role Server -ServerIP 192.168.1.1 -ClientIP 192.168.1.2

        # on the client laptop
        powershell -ExecutionPolicy Bypass -File tools\configure_network.ps1 `
            -Role Client -ServerIP 192.168.1.1 -ClientIP 192.168.1.2

.PARAMETER Role
    Server or Client. Decides which of -ServerIP / -ClientIP is applied here.

.PARAMETER ServerIP
    Static address for the GPU worker.

.PARAMETER ClientIP
    Static address for the laptop.

.PARAMETER AdapterAlias
    Optional adapter name or alias. Omit to use the first wired adapter.

.PARAMETER PrefixLength
    IPv4 prefix length. 24 for 192.168.1.x, 30 for a two-machine cable.

.PARAMETER Revert
    Switch the adapter back to DHCP.

.EXAMPLE
    .\configure_network.ps1 -Role Server
    .\configure_network.ps1 -Role Client
    .\configure_network.ps1 -Revert
#>
[CmdletBinding()]
param(
    [ValidateSet('Server', 'Client')]
    [string]$Role = 'Server',

    [string]$ServerIP = '192.168.1.1',
    [string]$ClientIP = '192.168.1.2',
    [string]$AdapterAlias,
    [ValidateRange(1, 32)]
    [int]$PrefixLength = 24,
    [switch]$Revert,
    [int]$Gateway
)

$ErrorActionPreference = 'Stop'

function Get-TargetAdapter {
    param([string]$Alias)
    $adapters = Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -or $_.Name -like '*Ethernet*' }
    if ($Alias) {
        $match = $adapters | Where-Object { $_.Name -eq $Alias -or $_.InterfaceDescription -like "*$Alias*" }
        if (-not $match) { throw "No active adapter matched '$Alias'. Run Get-NetAdapter to list them." }
        return $match | Select-Object -First 1
    }
    $wired = $adapters | Where-Object { $_.InterfaceDescription -notlike '*Wireless*' -and $_.Name -notlike '*Wi-Fi*' }
    if ($wired) { return $wired | Select-Object -First 1 }
    return $adapters | Select-Object -First 1
}

function Show-Status {
    param($Adapter)
    Write-Host ''
    Write-Host 'Current configuration' -ForegroundColor Cyan
    Write-Host '-----------------------'
    Get-NetIPConfiguration -InterfaceIndex $Adapter.ifIndex |
        Select-Object InterfaceAlias, IPv4Address, IPv4DefaultGateway |
        Format-List
}

$adapter = Get-TargetAdapter -Alias $AdapterAlias
$localIP = if ($Role -eq 'Server') { $ServerIP } else { $ClientIP }
$remoteIP = if ($Role -eq 'Server') { $ClientIP } else { $ServerIP }

Write-Host ''
Write-Host 'Distributed Task Offloading - static IP configuration' -ForegroundColor Green
Write-Host '-------------------------------------------------------'
Write-Host ("  role        : {0}" -f $Role)
Write-Host ("  adapter     : {0}  (ifIndex {1})" -f $adapter.Name, $adapter.ifIndex)
Write-Host ("  this host   : {0}/{1}" -f $localIP, $PrefixLength)
Write-Host ("  peer        : {0}" -f $remoteIP)
Write-Host ''

if ($Revert) {
    Set-NetIPInterface -InterfaceIndex $adapter.ifIndex -Dhcp Enabled
    Write-Host 'Reverted to DHCP.' -ForegroundColor Yellow
    Show-Status -Adapter $adapter
    return
}

# A two-machine cable needs routes on both sides; adding them explicitly avoids
# depending on a default gateway that will never exist on an isolated link.
$existing = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host 'Removing the existing IPv4 addresses on this adapter...' -ForegroundColor DarkGray
    $existing | Remove-NetIPAddress -Confirm:$false
}

Set-NetIPInterface -InterfaceIndex $adapter.ifIndex -Dhcp Disabled
New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress $localIP `
    -PrefixLength $PrefixLength -ErrorAction Stop | Out-Null

if ($Gateway) {
    New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress $remoteIP `
        -PrefixLength 30 -ErrorAction SilentlyContinue | Out-Null
}

Write-Host ("Assigned {0}/{1} to {2}" -f $localIP, $PrefixLength, $adapter.Name) -ForegroundColor Green

# Open the daemon's port on the worker so Windows Firewall does not silently drop
# the first connection attempt.
if ($Role -eq 'Server' -and -not $Gateway) {
    Write-Host ''
    Write-Host 'Opening TCP 7575 on the server firewall profile...' -ForegroundColor Cyan
    try {
        if (-not (Get-NetFirewallRule -DisplayName 'RDO Daemon Inbound' -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName 'RDO Daemon Inbound' `
                -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7575 `
                -Profile Private,Domain | Out-Null
            Write-Host 'Firewall rule created.' -ForegroundColor Green
        }
        else {
            Write-Host 'Firewall rule already present.'
        }
    }
    catch {
        Write-Warning "Could not add a firewall rule: $($_.Exception.Message)"
        Write-Warning 'Add it manually, or run the daemon as Administrator.'
    }
}

Write-Host ''
Write-Host 'Testing reachability' -ForegroundColor Cyan
$ping = Test-Connection -TargetName $remoteIP -Count 2 -Quiet -ErrorAction SilentlyContinue
if ($ping) {
    Write-Host ("  {0} responds to ICMP." -f $remoteIP) -ForegroundColor Green
}
else {
    Write-Host ("  {0} did not answer. Check the cable, the other adapter, and that" -f $remoteIP) -ForegroundColor Yellow
    Write-Host '  Windows Firewall on both machines, then try:'
    Write-Host ("    Test-NetConnection {0} -Port 7575" -f $remoteIP) -ForegroundColor Yellow
}

Show-Status -Adapter $adapter