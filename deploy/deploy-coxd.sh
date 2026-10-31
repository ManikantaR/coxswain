#!/usr/bin/env bash
# Ship coxd to the NAS and (re)build it. Volumes persist the store, creds, and repos,
# so this is safe to re-run for updates. One-time auth + repo-clone: see deploy/README.md.
set -euo pipefail

NAS_HOST="${NAS_HOST:-nas}"
REMOTE_DIR="${REMOTE_DIR:-coxd}"        # dir in the ssh user's home on the NAS
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[coxd-deploy] syncing coxswain → ${NAS_HOST}:${REMOTE_DIR}/"
# macOS ships openrsync (proto 29), which is incompatible with the NAS's rsync 3.4
# ("invalid path"). tar-over-ssh is version-agnostic and works everywhere.
COPYFILE_DISABLE=1 tar czf - -C "${SRC}" \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.egg-info' --exclude='./cox' --exclude='./data' \
    --exclude='./deploy/.env' --exclude='./deploy/.env.*' . \
  | ssh "${NAS_HOST}" "mkdir -p ${REMOTE_DIR} && tar xzf - -C ${REMOTE_DIR}"

# The NAS deploy/.env is the ONLY copy of the live secrets (GH_TOKEN, COXD_TG_*). It is
# excluded from the tar above: shipping the workstation's (gitignored) deploy/.env used
# to silently overwrite it — the 2026-10-31 rebuild wiped the Telegram settings.
ssh "${NAS_HOST}" "test -f ${REMOTE_DIR}/deploy/.env" \
  || { echo "[coxd-deploy] ${REMOTE_DIR}/deploy/.env missing on the NAS — create it (see deploy/README.md)" >&2; exit 3; }

echo "[coxd-deploy] build + up on the NAS"
ssh "${NAS_HOST}" "cd ${REMOTE_DIR}/deploy && docker compose -f docker-compose.coxd.yml --env-file .env up -d --build"

echo "[coxd-deploy] done → board https://coxd.home.manikantar.com/"
echo "[coxd-deploy] first time? run the auth + clone steps in deploy/README.md (claude login is yours)."
