@echo off
title ESTrader A.I. - Port 5007
cd /d C:\Users\abc\Desktop\ESTraderAI
start /min "ESTrader A.I. Dashboard" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_es.py
start /min "ESTrader A.I. Engine" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_es.py
