#!/usr/bin/env bash
# Install or update: asks for the site address once, writes .env, starts the ready image.
# Run again to update (git pull first). Delete .env to answer the questions again.
set -euo pipefail
cd "$(dirname "$0")"

docker compose version >/dev/null 2>&1 ||
  { echo "Needs Docker with compose: https://docs.docker.com/engine/install/"; exit 1; }
docker info >/dev/null 2>&1 || docker() { sudo docker "$@"; }  # user not in the docker group

if [ -f .env ]; then
  echo "Using existing .env (delete it to answer the questions again)."
else
  ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  echo "Labels will carry the site address, and printed labels can't be changed later."
  echo "  1) IP address - works right away; labels break if the server's IP changes"
  echo "  2) Name, e.g. inv.lan - survives an IP change; needs a DNS record in your router"
  read -rp "Address by [1]: " kind
  if [ "${kind:-1}" = 2 ]; then
    read -rp "Name: " host
  else
    read -rp "IP [${ip}]: " host
    host=${host:-$ip}
  fi
  [ -n "$host" ] || { echo "No address given."; exit 1; }
  read -rp "Port [80]: " port
  port=${port:-80}
  base="http://$host"; [ "$port" = 80 ] || base="$base:$port"  # Chrome's NFC flag must match host:port

  z=$(date +%z)  # +0300 -> POSIX <MSK>-3: sign inverted, works without tzdata in the image
  sign=-; [ "${z:0:1}" = - ] && sign=+
  tz="<$(date +%Z)>$sign$((10#${z:1:2}))"; [ "${z:3:2}" = 00 ] || tz="$tz:${z:3:2}"

  sed -e "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$base|" -e "s|^PORT=.*|PORT=$port|" \
      -e "s|^TZ=.*|TZ=$tz|" .env.example > .env
  if [ "$kind" = 2 ] && ! getent hosts "$host" >/dev/null 2>&1; then
    echo "Note: $host doesn't resolve yet. Add a DNS record $host -> ${ip:-the server IP} in your router, see docs/setup.md"
  fi
fi

docker compose pull || echo "No ready image, building one here (a few minutes)."
docker compose up -d
port=$(sed -n 's/^PORT=//p' .env); base=$(sed -n 's/^PUBLIC_BASE_URL=//p' .env)
base=${base:-http://localhost:${port:-8000}}
echo
echo "Site:  $base"
echo "Phone: ${base}/phone - app install and NFC tag writing"
