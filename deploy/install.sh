#!/usr/bin/env bash
# התקנה על שרת Ubuntu חדש (22.04/24.04). הרצה כ-root:
#   curl -fsSL https://raw.githubusercontent.com/YosefHatav/Drone-survey-cad/main/deploy/install.sh | bash -s -- DOMAIN PASSWORD
# DOMAIN = הדומיין שלך (רשומת A מצביעה לכתובת ה-IP של השרת), PASSWORD = סיסמת כניסה לאתר
set -euo pipefail
DOMAIN="${1:?usage: install.sh DOMAIN PASSWORD}"
PASSWORD="${2:?usage: install.sh DOMAIN PASSWORD}"
apt-get update -y
apt-get install -y ca-certificates curl git ufw
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh; fi
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
mkdir -p /opt && cd /opt
if [ -d drone-survey-cad ]; then git -C drone-survey-cad pull; else git clone https://github.com/YosefHatav/Drone-survey-cad.git drone-survey-cad; fi
cd drone-survey-cad/deploy
printf 'DOMAIN=%s\nAPP_PASSWORD=%s\n' "$DOMAIN" "$PASSWORD" > .env
chmod 600 .env
docker compose -f docker-compose.prod.yml --env-file .env up -d --build
echo "האתר יעלה בעוד כמה דקות בכתובת https://$DOMAIN"
