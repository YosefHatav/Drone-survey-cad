#!/usr/bin/env bash
# עדכון לגרסה האחרונה מהריפו
set -euo pipefail
cd /opt/drone-survey-cad && git pull
cd deploy && docker compose -f docker-compose.prod.yml --env-file .env up -d --build
