@echo off
cd /d %~dp0
py -m pip install -q -r requirements.txt
start http://localhost:8000
py -m uvicorn app:app --app-dir backend --host 0.0.0.0 --port 8000
