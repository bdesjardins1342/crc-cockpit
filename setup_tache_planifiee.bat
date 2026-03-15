@echo off
echo Creation de la tache planifiee CRC-Cockpit-SEAO-Sync...

schtasks /create /tn "CRC-Cockpit-SEAO-Sync" ^
  /tr "C:\Users\BenoitDesjardins\Documents\Claude\crc-cockpit\sync_seao.bat" ^
  /sc weekly /d MON /st 06:00 ^
  /ru "%USERNAME%" ^
  /f

if %errorlevel% == 0 (
  echo Tache creee avec succes : chaque lundi a 06h00
) else (
  echo ERREUR - Relancer ce script en tant qu'Administrateur
)
pause
