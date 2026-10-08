# Lance l'IDS reseau en mode detection. ADMIN requis (netsh pose les regles pare-feu en mode
# blocage/leurre). En session elevee, les variables d'environnement UTILISATEUR ne sont pas chargees
# automatiquement : on recharge ici le jeton de l'API d'ingestion depuis le registre utilisateur
# (sans l'exposer en clair sur une ligne de commande).
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

# Recupere une variable d'env UTILISATEUR. En session elevee, l'admin (marci) n'a pas les variables
# de ton compte standard : on va alors la chercher dans la ruche de registre de l'utilisateur
# interactif, qui est chargee tant que ta session est ouverte (HKEY_USERS\<ton SID>\Environment).
# Rien n'est ecrit en clair ni passe sur une ligne de commande.
function Get-UserVar($name) {
    $v = [Environment]::GetEnvironmentVariable($name, "User")
    if ($v) { return $v }
    foreach ($hive in Get-ChildItem Registry::HKEY_USERS -ErrorAction SilentlyContinue) {
        $key = "Registry::$($hive.Name)\Environment"
        if (Test-Path $key) {
            $val = (Get-ItemProperty $key -Name $name -ErrorAction SilentlyContinue).$name
            if ($val) { return $val }
        }
    }
    return $null
}

foreach ($v in @("SENTINEL_IDS_TOKEN")) {
    $val = Get-UserVar $v
    if ($val) { Set-Item "Env:$v" $val }
    else { Write-Host "ATTENTION : $v introuvable (ta session utilisateur est-elle ouverte ?)" -ForegroundColor Red }
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
