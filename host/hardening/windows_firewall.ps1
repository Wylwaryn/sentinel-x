# Sentinel-X - pare-feu Windows en "mode pentest" (reversible).
#
#   .\windows_firewall.ps1             simulation : affiche ce qui serait change (aucune modification)
#   .\windows_firewall.ps1 -Apply      applique (PowerShell ADMINISTRATEUR)
#   .\windows_firewall.ps1 -Restore    remet le pare-feu exactement comme avant (ADMINISTRATEUR)
#
# Effet de -Apply :
#   1. sauvegarde complete du pare-feu (netsh advfirewall export) + liste des regles desactivees ;
#   2. desactive toutes les regles ENTRANTES "Autoriser" du profil Public (jeux, adb, Node, Docker,
#      diffusion sans fil...), sauf la gestion reseau de base de Windows (DHCP client, ICMPv6...) ;
#   3. cree des regles Sentinel-X limitees au reseau de la table (192.168.137.0/24) :
#        8883 MQTTS et 443 dashboard (redirections VirtualBox, fenetre OU headless), 67 DHCP et 53 DNS.
# Les regles de blocage de l'IDS (SentinelX-IDS-*) ne sont jamais touchees.
# Messages sans accents : PowerShell 5.1 lit les scripts sans BOM en ANSI.
param([switch]$Apply, [switch]$Restore)

$ErrorActionPreference = "Stop"
$Subnet = "192.168.137.0/24"
$Prefix = "Sentinel-X "
$StateDir = Join-Path $PSScriptRoot "backup"
$BackupWfw = Join-Path $StateDir "pare-feu-avant-pentest.wfw"
$DisabledList = Join-Path $StateDir "regles-desactivees.json"
$CoreGroup = "@FirewallAPI.dll,-25000"   # groupe "Gestion reseau de base" (Core Networking)

function Test-Admin {
    $id = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $id.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (($Apply -or $Restore) -and -not (Test-Admin)) {
    Write-Host "Ce mode demande un PowerShell lance en ADMINISTRATEUR." -ForegroundColor Red
    exit 1
}

if ($Restore) {
    if (-not (Test-Path $DisabledList)) { Write-Host "Aucune sauvegarde trouvee : rien a restaurer."; exit 0 }
    Get-NetFirewallRule -DisplayName "$Prefix*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $names = Get-Content $DisabledList -Raw | ConvertFrom-Json
    foreach ($n in $names) { Get-NetFirewallRule -Name $n -ErrorAction SilentlyContinue | Enable-NetFirewallRule }
    Write-Host ("Restaure : {0} regles reactivees, regles Sentinel-X supprimees." -f $names.Count) -ForegroundColor Green
    Write-Host "Sauvegarde complete toujours disponible : netsh advfirewall import `"$BackupWfw`""
    exit 0
}

# Programmes qui ecoutent pour les redirections NAT de VirtualBox (8883, 443) : VirtualBoxVM.exe si la VM
# est ouverte en fenetre, VBoxHeadless.exe si elle tourne sans fenetre (startvm --type headless).
# Les deux sont autorises : une regle liee au seul VirtualBoxVM.exe bloquerait l'ESP et le dashboard
# des que la VM tourne en headless (constate le 7 oct.).
$vboxDir = "C:\Program Files\Oracle\VirtualBox"
$found = Get-NetFirewallApplicationFilter | Where-Object { $_.Program -match "virtualboxvm\.exe$" } |
         Select-Object -First 1 -ExpandProperty Program
if ($found) { $vboxDir = Split-Path -Parent $found }
$vboxProgs = @((Join-Path $vboxDir "VirtualBoxVM.exe"), (Join-Path $vboxDir "VBoxHeadless.exe"))

$toDisable = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True |
    Where-Object { $_.Profile -match "Public|Any" -and $_.Group -ne $CoreGroup -and
                   $_.DisplayName -notlike "$Prefix*" -and $_.DisplayName -notlike "SentinelX-IDS-*" }

$newRules = @()
foreach ($prog in $vboxProgs) {
    $short = [IO.Path]::GetFileNameWithoutExtension($prog)
    $newRules += @{ Name = "MQTTS 8883 (ESP, vision) - $short"; Protocol = "TCP"; Port = "8883"; Program = $prog }
    $newRules += @{ Name = "HTTPS 443 (dashboard) - $short";    Protocol = "TCP"; Port = "443";  Program = $prog }
    # Leurre honeypot (VM "honeypot") : doit etre joignable sur le hotspot pour piéger les attaquants.
    $newRules += @{ Name = "Honeypot MySQL 3306 (leurre) - $short"; Protocol = "TCP"; Port = "3306"; Program = $prog }
    $newRules += @{ Name = "Honeypot HTTP 8080 (leurre) - $short";  Protocol = "TCP"; Port = "8080"; Program = $prog }
}
$newRules += @(
    @{ Name = "DHCP point d'acces";       Protocol = "UDP"; Port = "67";   Program = $null },
    @{ Name = "DNS point d'acces UDP";    Protocol = "UDP"; Port = "53";   Program = $null },
    @{ Name = "DNS point d'acces TCP";    Protocol = "TCP"; Port = "53";   Program = $null }
)

Write-Host ("Regles entrantes 'Autoriser' du profil Public a desactiver : {0}" -f @($toDisable).Count) -ForegroundColor Yellow
$toDisable | Sort-Object DisplayName | ForEach-Object { Write-Host ("  - " + $_.DisplayName) }
Write-Host "Regles Sentinel-X a creer (source $Subnet uniquement) :" -ForegroundColor Yellow
$newRules | ForEach-Object { Write-Host ("  + {0} : {1} {2}" -f $_.Name, $_.Protocol, $_.Port) }

if (-not $Apply) {
    Write-Host "`nSIMULATION : rien n'a ete modifie. Relancer avec -Apply (administrateur) pour appliquer." -ForegroundColor Cyan
    exit 0
}

New-Item -ItemType Directory -Force $StateDir | Out-Null
if (-not (Test-Path $BackupWfw)) { netsh advfirewall export "$BackupWfw" | Out-Null }
$already = @()
if (Test-Path $DisabledList) { $already = @(Get-Content $DisabledList -Raw | ConvertFrom-Json) }
$names = @($already) + @($toDisable | ForEach-Object Name) | Select-Object -Unique
$names | ConvertTo-Json | Set-Content -Encoding UTF8 $DisabledList

$toDisable | Disable-NetFirewallRule
foreach ($r in $newRules) {
    $display = $Prefix + $r.Name
    Get-NetFirewallRule -DisplayName $display -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $params = @{ DisplayName = $display; Direction = "Inbound"; Action = "Allow"; Profile = "Any";
                 Protocol = $r.Protocol; LocalPort = $r.Port; RemoteAddress = $Subnet }
    if ($r.Program) { $params.Program = $r.Program }
    New-NetFirewallRule @params | Out-Null
}
Write-Host ("`nApplique : {0} regles desactivees, {1} regles Sentinel-X creees." -f @($toDisable).Count, $newRules.Count) -ForegroundColor Green
Write-Host "A verifier : un telephone se connecte au point d'acces (DHCP), l'ESP joint 8883, le dashboard s'ouvre."
Write-Host "Retour arriere : .\windows_firewall.ps1 -Restore"
