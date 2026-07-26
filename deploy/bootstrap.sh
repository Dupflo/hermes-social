#!/usr/bin/env bash
# Hermes Social — fresh-VPS bootstrap.
# Assumes: Ubuntu/Debian host with Docker installed, run from deploy/.
set -euo pipefail

cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "Docker is required — install it first."; exit 1; }
command -v make   >/dev/null || { apt-get update -qq && apt-get install -y -qq make; }

# 1. Secrets
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo ">>> deploy/.env created from template — fill it in, then re-run this script."
  exit 0
fi

# 2. Images
( cd .. && make core image )
docker image inspect "camofox-browser:135.0.1-$(uname -m)" >/dev/null 2>&1 \
  || ( cd .. && make camofox )

# 3. Up
docker compose up -d
echo
docker compose ps
echo
echo ">>> Done. Next steps:"
echo "    - Hermes UI:  ssh -L 4860:127.0.0.1:4860 root@<this-vps>  →  http://localhost:4860"
echo "    - noVNC:      ssh -L 6080:127.0.0.1:6080 root@<this-vps>  →  http://localhost:6080"
echo "    - Enable the social skills in data/config.yaml:"
echo "        skills:"
echo "          external_dirs:"
echo "            - /opt/hermes-social/skills"
