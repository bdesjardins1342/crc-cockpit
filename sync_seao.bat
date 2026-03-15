@echo off
cd /d "C:\Users\BenoitDesjardins\Documents\Claude\crc-cockpit"
echo [%date% %time%] Debut sync SEAO >> logs\sync_seao.log
python seao_scraper.py --sync >> logs\sync_seao.log 2>&1
echo [%date% %time%] Fin sync SEAO >> logs\sync_seao.log
