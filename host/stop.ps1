# Sentinel-X - arret des services du PC hote (vision + maintenance predictive).
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
  Where-Object { $_.CommandLine -match 'sentinel_(vision|predictive)' } |
  ForEach-Object { Write-Host ("Arret PID " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }
Write-Host "Services arretes."
