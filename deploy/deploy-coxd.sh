#!/usr/bin/env bash
# Ship coxd to the NAS and (re)build it. Volumes persist the store, creds, and repos,
# so this is safe to re-run for updates. One-time auth + repo-clone: see deploy/README.md.
set -euo pipefail

NAS_HOST="${NAS_HOST:-nas}"
REMOTE_DIR="${REMOTE_DIR:-coxd}"        # dir in the ssh user's home on the NAS
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[coxd-deploy] syncing coxswain → ${NAS_HOST}:${REMOTE_DIR}/"
ssh "${NAS_HOST}" "mkdir -p ${REMOTE_DIR}"      # rsync 3.4 rejects a missing dest dir
rsync -az --delete \
  --exclude '.git' --exclude '**/.venv' --exclude '**/__pycache__' \
  --exclude '**/*.egg-info' --exclude 'data' --exclude 'cox' \
  "${SRC}/" "${NAS_HOST}:${REMOTE_DIR}/"

echo "[coxd-deploy] build + up on the NAS"
ssh "${NAS_HOST}" "cd ${REMOTE_DIR}/deploy && docker compose -f docker-compose.coxd.yml --env-file .env up -d --build"

echo "[coxd-deploy] done → board http://${NAS_HOST}:8791/"
echo "[coxd-deploy] first time? run the auth + clone steps in deploy/README.md (claude login is yours)."
