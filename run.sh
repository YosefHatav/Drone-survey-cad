#!/usr/bin/env bash
# הפעלה: ./run.sh  ואז לפתוח http://localhost:8000
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
exec python3 -m uvicorn app:app --app-dir backend --host 0.0.0.0 --port 8000
