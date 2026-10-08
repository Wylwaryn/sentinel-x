# Lance l'IDS reseau en mode detection. ADMIN requis (netsh pose les regles pare-feu en mode
# blocage/leurre). En session elevee, les variables d'environnement UTILISATEUR ne sont pas chargees
# automatiquement : on recharge ici le jeton de l'API d'ingestion depuis le registre utilisateur
# (sans l'exposer en clair sur une ligne de commande).
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

foreach ($v in @("SENTINEL_IDS_TOKEN")) {
    $val = [Environment]::GetEnvironmentVariable($v, "User")
    if ($val) { Set-Item "Env:$v" $val }
}

$ids  = $PSScriptRoot                                   # ...\host\ids
$root = Split-Path -Parent (Split-Path -Parent $ids)    # ...\Workshop M1
$py   = Join-Path $root ".venv\Scripts\python.exe"
$log  = Join-Path $ids "logs\ids-live.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

Set-Location $ids
Write-Host "IDS en detection (mode depuis config.json). Ctrl+C pour arreter. Journal : $log"
# Tee : affichage live dans cette fenetre ET journal pour la supervision.
& $py -u sentinel_ids.py detect 2>&1 | Tee-Object -FilePath $log
