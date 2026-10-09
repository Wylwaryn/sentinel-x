# Sentinel-X — démarrage des services du PC hôte (vision + maintenance prédictive).
# À lancer après chaque redémarrage de Windows. Double-clic : start.bat (à côté).
#
# - Arrête d'abord les instances existantes (deux process sur la même webcam = échec caméra).
# - Relance en arrière-plan, journaux dans host\logs\.
# - La vision prend la webcam USB par son NOM (host\vision\config.json) et attend qu'elle soit
#   présente : elle ne bascule jamais sur la caméra intégrée du portable.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # racine du projet (parent de host\)
$py   = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path $py)) { Write-Host "Python introuvable : $py" -ForegroundColor Red; exit 1 }

# 0) Verrou mono-instance : empeche DEUX start.bat lances en meme temps de creer des doublons
#    (constate le 9 oct. : 2 visions + 2 predictives, qui se disputent la webcam USB).
$mutex = New-Object System.Threading.Mutex($false, "Global\SentinelX-Start")
if (-not $mutex.WaitOne(0)) {
  Write-Host "Un demarrage est deja en cours (start.bat lance deux fois ?). Rien a faire." -ForegroundColor Yellow
  exit 0
}
try {

# 1) Arrêter les instances existantes (et attendre qu'elles soient vraiment parties)
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -match 'sentinel_(vision|predictive)' } |
  ForEach-Object { Write-Host ("Arret de l'ancienne instance PID " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }
for ($i = 0; $i -lt 10; $i++) {
  Start-Sleep -Milliseconds 300
  $reste = @(Get-CimInstance Win32_Process -Filter "name='python.exe'" |
             Where-Object { $_.CommandLine -match 'sentinel_(vision|predictive)' })
  if ($reste.Count -eq 0) { break }
}

function Start-Svc($name, $dir, $scriptArgs) {
  $wd  = Join-Path $root $dir
  $out = Join-Path $logs "$name.log"
  $err = Join-Path $logs "$name.err.log"
  Start-Process -FilePath $py -ArgumentList (@("-u") + $scriptArgs) -WorkingDirectory $wd `
    -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
  Write-Host ("  $name lance -> $out")
}

Write-Host "`nDemarrage des services Sentinel-X..." -ForegroundColor Cyan
Start-Svc "vision"     "host\vision"     @("sentinel_vision.py", "--headless")
Start-Svc "predictive" "host\predictive" @("sentinel_predictive.py", "detect")

# 2) Petit contrôle : la vision a-t-elle bien pris la webcam USB ?
Write-Host "`nVerification (chargement de YOLO ~15 s)..." -ForegroundColor Cyan
$visionLog = Join-Path $logs "vision.log"
$cam = $null
for ($i = 0; $i -lt 10; $i++) {
  Start-Sleep -Seconds 3
  if (Test-Path $visionLog) {
    $cam = Select-String -Path $visionLog -Pattern "\[CAM" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($cam) { break }
  }
}
if ($cam) { Write-Host ("  " + $cam.Line) -ForegroundColor Green }
else      { Write-Host "  (pas encore de ligne CAMERA ; voir $visionLog)" -ForegroundColor Yellow }

$running = (Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -match 'sentinel_(vision|predictive)' }).Count
Write-Host ("`n$running service(s) en cours. Journaux : $logs") -ForegroundColor Cyan
Write-Host "Pour arreter : host\stop.ps1 (ou stop.bat)."

}
finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
