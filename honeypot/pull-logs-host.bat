@echo off
REM Wrapper hote : recupere + dechiffre les logs du honeypot (appele par le Planificateur, 10 min).
REM Adapte le chemin de bash.exe si ton Git est installe ailleurs.
cd /d "%~dp0"
"C:\Users\Wyllwaryn_User\AppData\Local\Programs\Git\bin\bash.exe" -lc "./pull-logs.sh >> \"$HOME/honeypot-logs/pull.log\" 2>&1"
